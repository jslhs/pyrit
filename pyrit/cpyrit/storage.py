# -*- coding: UTF-8 -*-
#
#    Copyright 2008-2010, Lukas Lueg, lukas.lueg@gmail.com
#
#    This file is part of Pyrit.
#
#    Pyrit is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Pyrit is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Pyrit.  If not, see <http://www.gnu.org/licenses/>.

"""EssidStore and PasswordStore are the primary storage classes. Details of
   their implementation are reasonably well hidden behind the concept of
   key:value interaction.

"""

from __future__ import with_statement

# prevent call to socket.getfqdn
import BaseHTTPServer
def fast_address_string(self):
    return '%s' % self.client_address[0]
BaseHTTPServer.BaseHTTPRequestHandler.address_string = fast_address_string
del fast_address_string

import hashlib
import os
import random
import re
import SimpleXMLRPCServer
import struct
import sys
import threading
import xmlrpclib
import zlib
try:
    import sqlalchemy as sql
except ImportError:
    pass
else:
    from sqlalchemy import orm
    if hasattr(sql, 'LargeBinary'):
        UniversalBinary = sql.LargeBinary
    else:
        UniversalBinary = sql.Binary

import config
import util

MAX_WORKUNIT_SIZE = int(config.cfg['workunit_size'])
if MAX_WORKUNIT_SIZE < 1 or MAX_WORKUNIT_SIZE > 1000000:
    raise ValueError("Invalid 'workunit_size' in configuration")

URL_GROUPER = re.compile('(?P<protocol>\w+)://(((?P<user>\w+):?(?P<passwd>\w+)?@)?(?P<tail>.+))?')

def pruneURL(url):
    """Remove user/passwd from a storage-url"""
    match = URL_GROUPER.match(url)
    if match is None:
        return url
    else:
        url_parts = match.groupdict()
        protocol = url_parts['protocol']
        if protocol is None:
            protocol = ''
        tail = url_parts['tail']
        if tail is None:
            tail = ''
        return "%s://%s" % (protocol, tail)

def getStorage(url):
    if not '://' in url:
        raise ValueError("URL must be of form [protocol]://" \
                         "[connection-string]")
    protocol, conn = url.split('://', 1)
    if protocol == 'file':
        return FSStorage(url)
    elif protocol == 'http':
        return RPCStorage(url)
    elif protocol in ('sqlite', 'mysql', 'postgres', 'postgresql', \
                      'oracle', 'mssql', 'firebird'):
        if 'sqlalchemy' not in sys.modules:
            raise util.SqlalchemyImportError("SQLAlchemy seems to be " \
                                             "unavailable.")
        return SQLStorage(url)
    else:
        raise RuntimeError("The protocol '%s' is unsupported." % protocol)


class StorageError(IOError):
    pass


class DigestError(StorageError):
    pass


class PasswordCollection(object):
    """An abstract collection of passwords."""

    def __init__(self, collection=None):
        if collection is not None:
            self.collection = collection

    def __len__(self):
        return len(self.collection)

    def __iter__(self):
        return self.collection.__iter__()


class BasePYR_Buffer(object):
    """The common parts of the PYRT- and PYR2-binary format."""
    pyr_head = '<4sH'
    pyr_len = struct.calcsize(pyr_head)

    def __init__(self, essid=None, results=None):
        self.results = results
        self.essid = essid
        self._unpackLock = threading.Lock()

    def unpack(self, buf):
        if len(buf) < self.pyr_len:
            raise StorageError("Buffer too short")
        self._magic, essidlen = struct.unpack(self.pyr_head, buf[:self.pyr_len])
        if self._magic == 'PYR2':
            self._delimiter = '\n'
        elif self._magic == 'PYRT':
            self._delimiter = '\00'
        else:
            raise StorageError("Not a PYRT- or PYR2-buffer.")
        headfmt = "<%ssi16s" % (essidlen, )
        headsize = struct.calcsize(headfmt)
        header = buf[self.pyr_len:self.pyr_len + headsize]
        if len(header) != headsize:
            raise StorageError("Invalid header size")
        header = struct.unpack(headfmt, header)
        self.essid, self._numElems, self._digest = header
        pmkoffset = self.pyr_len + headsize
        pwoffset = pmkoffset + self._numElems * 32
        self._pwbuffer = buf[pwoffset:]
        self._pmkbuffer = buf[pmkoffset:pwoffset]
        if len(self._pmkbuffer) % 32 != 0:
            raise StorageError("pmkbuffer seems truncated")

    def _unpack(self):
        with self._unpackLock:
            if hasattr(self, '_pwbuffer'):
                pwbuffer = zlib.decompress(self._pwbuffer).split(self._delimiter)
                assert len(pwbuffer) == self._numElems
                md = hashlib.md5()
                md.update(self.essid)
                if self._magic == 'PYR2':
                    md.update(self._pmkbuffer)
                    md.update(self._pwbuffer)
                else:
                    md.update(self._pmkbuffer)
                    md.update(''.join(pwbuffer))
                if md.digest() != self._digest:
                    raise DigestError("Digest check failed")
                self.results = zip(pwbuffer, util.grouper(self._pmkbuffer, 32))
                assert len(self.results) == self._numElems
                del self._pwbuffer
                del self._digest
                del self._magic
                del self._delimiter

    def __getitem__(self, idx):
        self._unpack()
        try:
            return self.results[idx]
        except IndexError:
            raise IndexError("Instance has %i results, index %i invalid." % \
                             (len(self.results), idx))

    def __len__(self):
        if not hasattr(self, '_numElems'):
            return len(self.results)
        else:
            return self._numElems

    def __iter__(self):
        self._unpack()
        return self.results.__iter__()

    def getpmkbuffer(self):
        return buffer(self._pmkbuffer)


class PYRT_Buffer(BasePYR_Buffer):
    pass

class PYR2_Buffer(BasePYR_Buffer):

    def pack(self):
        pws, pmks = zip(*self.results)
        pwbuffer = zlib.compress('\n'.join(pws), 1)
        pmkbuffer = ''.join(pmks)
        md = hashlib.md5()
        md.update(self.essid)
        md.update(pmkbuffer)
        md.update(pwbuffer)
        essidlen = len(self.essid)
        b = struct.pack('<4sH%ssi%ss' % (essidlen, md.digest_size), 'PYR2', \
                        essidlen, self.essid, len(pws), md.digest())
        return b + pmkbuffer + pwbuffer


class PAWD_Buffer(PasswordCollection):

    def unpack(self, buf):
        if buf[:4] != "PAWD":
            raise StorageError("Not a PAWD-buffer.")
        md = hashlib.md5()
        inp = tuple(buf[4 + md.digest_size:].split('\00'))
        md.update(''.join(inp))
        if buf[4:4 + md.digest_size] != md.digest():
            raise DigestError("Digest check failed.")
        self.collection = inp
        self.key = md.hexdigest()


class PAW2_Buffer(PasswordCollection):

    def pack(self):
        b = zlib.compress('\n'.join(self.collection), 1)
        md = hashlib.md5(b)
        self.key = md.hexdigest()
        return (md.hexdigest(), 'PAW2' + md.digest() + b)

    def unpack(self, buf):
        if buf[:4] != "PAW2":
            raise StorageError("Not a PAW2-buffer.")
        md = hashlib.md5()
        md.update(buf[4 + md.digest_size:])
        if md.digest() != buf[4:4 + md.digest_size]:
            raise DigestError("Digest check failed")
        inp = tuple(zlib.decompress(buf[4 + md.digest_size:]).split('\n'))
        self.collection = inp
        self.key = md.hexdigest()


class ESSIDStore(object):
    """Storage-class responsible for ESSID and PMKs.

       Callers can use the iterator to cycle over available ESSIDs.
       Results are indexed by keys and returned as iterables of tuples. The
       keys may be received from .iterkeys() or from PasswordStore.
    """

    def iterresults(self, essid):
        """Iterate over all results currently stored for the given ESSID."""
        for key in self.iterkeys(essid):
            yield self[essid, key]

    def iteritems(self, essid):
        """Iterate over all keys and results currently stored for the given
           ESSID.
        """
        for key in self.iterkeys(essid):
            yield (key, self[essid, key])


class PasswordStore(object):
    """Storage-class responsible for passwords.

       Passwords are indexed by keys and are returned as iterables.
       The iterator cycles over all available keys.
    """
    h1_list = ["%02.2X" % i for i in xrange(256)]
    del i

    def __init__(self):
        self.pwbuffer = {}
        self.unique_check = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.flush_buffer()
        return False

    def flush_buffer(self):
        """Flush all passwords currently buffered to the storage.

           For efficiency reasons this function should not be called if the
           caller wants to add more passwords in the foreseeable future.
        """
        for pw_h1, pw_bucket in self.pwbuffer.iteritems():
            self._flush_bucket(pw_h1, pw_bucket)
            self.pwbuffer[pw_h1] = (set if self.unique_check else list)()

    def store_password(self, passwd):
        """Add the given password to storage.

           Passwords passed to this function are buffered in memory for better
           performance and efficiency. It is the caller's responsibility to
           call .flush_buffer() (or use the context-manager) when he is done.
        """
        passwd = passwd.strip('\r\n')
        if len(passwd) < 8 or len(passwd) > 63:
            return
        pw_h1 = PasswordStore.h1_list[hash(passwd) & 0xFF]
        if self.unique_check:
            pw_bucket = self.pwbuffer.setdefault(pw_h1, set())
            pw_bucket.add(passwd)
        else:
            pw_bucket = self.pwbuffer.setdefault(pw_h1, list())
            pw_bucket.append(passwd)
        if len(pw_bucket) >= MAX_WORKUNIT_SIZE:
            self._flush_bucket(pw_h1, pw_bucket)
            self.pwbuffer[pw_h1] = (set if self.unique_check else list)()

    def iterkeys(self):
        """Equivalent to self.__iter__"""
        return self.__iter__()

    def iterpasswords(self):
        """Iterate over all available passwords-sets."""
        for key in self:
            yield self[key]

    def iteritems(self):
        """Iterate over all keys and password-sets."""
        for key in self:
            yield (key, self[key])


class Storage(object):

    def __delitem__(self, key):
        for essid in self.essids:
            if self.essids.containskey(essid, key):
                del self.essids[essid, key]
        del self.passwords[key]

    def iterresults(self, essid):
        return self.essids.iterresults(essid)

    def iterpasswords(self):
        return self.passwords.iterpasswords()


class FSStorage(Storage):
    """Storage-class that uses the filesystem

       Connection strings must be in the form of file://...
       The special character '~' is automatically expanded to the user's
       home directory.
    """

    def __init__(self, url):
        if not url.startswith('file://'):
            raise ValueError("Connection-string must be of form 'file://'")
        path = url.split('file://')[1]
        if path == '':
            path = os.path.join('~', '.pyrit', 'blobspace')
        path = os.path.expanduser(path)
        self.essids = FSEssidStore(os.path.join(path, 'essid'))
        self.passwords = FSPasswordStore(os.path.join(path, 'password'))

    def iterresults(self, essid):
        return self.essids.iterresults(essid)

    def iterpasswords(self):
        return self.passwords.iterpasswords()

    def getStats(self):
        essid_results = dict.fromkeys(self.essids, 0)
        pwcount = 0
        for i, key in enumerate(self.passwords):
            pwsize = self.passwords.size(key)
            pwcount += pwsize
            for essid in essid_results:
                if self.essids.containskey(essid, key):
                    essid_results[essid] += pwsize
        return (pwcount, essid_results)


class FSEssidStore(ESSIDStore):

    def __init__(self, basepath):
        ESSIDStore.__init__(self)
        self.basepath = basepath
        if not os.path.exists(self.basepath):
            os.makedirs(self.basepath)
        self.essids = {}
        for essid_hash in os.listdir(self.basepath):
            essidpath = os.path.join(self.basepath, essid_hash)
            with open(os.path.join(essidpath, 'essid'), 'rb') as f:
                essid = f.read()
            if essid_hash == hashlib.md5(essid).hexdigest()[:8]:
                self.essids[essid] = (essidpath, {})
                for pyrfile in os.listdir(essidpath):
                    if pyrfile.endswith('.pyr'):
                        self.essids[essid][1][pyrfile[:len(pyrfile)-4]] = \
                                            os.path.join(essidpath, pyrfile)
            else:
                print >> sys.stderr, "ESSID %s is corrupted." % essid_hash

    def __getitem__(self, (essid, key)):
        """Receive a iterable of (password,PMK)-tuples stored under
           the given ESSID and key.

           Returns a empty iterable if the key is not stored. Raises KeyError
           if the ESSID is not stored.
        """
        try:
            fname = self.essids[essid][1][key]
        except IndexError:
            raise IndexError("No result for ESSID:Key (%s:%s)" % (essid, key))
        else:
            with open(fname, 'rb') as f:
                buf = f.read()
            if buf.startswith('PYR2'):
                results = PYR2_Buffer()
            elif buf.startswith('PYRT'):
                results = PYRT_Buffer()
            else:
                raise StorageError("Header for '%s' unknown." % (fname,))
            results.unpack(buf)
            if results.essid != essid:
                raise StorageError("Invalid ESSID in result-collection")
            return results

    def __setitem__(self, (essid, key), results):
        """Store a iterable of (password,PMK)-tuples under the given
           ESSID and key.
        """
        if essid not in self.essids:
            raise KeyError("ESSID not in store.")
        filename = os.path.join(self.essids[essid][0], key) + '.pyr'
        with open(filename, 'wb') as f:
            f.write(PYR2_Buffer(essid, results).pack())
        self.essids[essid][1][key] = filename

    def __len__(self):
        """Return the number of ESSIDs currently stored."""
        return len(self.essids)

    def __iter__(self):
        """Iterate over all essids currently stored."""
        return self.essids.__iter__()

    def __contains__(self, essid):
        """Return True if the given ESSID is currently stored."""
        return essid in self.essids

    def __delitem__(self, item):
        """Delete the given ESSID and all results from the storage."""
        if type(item) is tuple:
            essid, key = item
            essid_root, pyrfiles = self.essids[essid]
            fname = pyrfiles.pop(key)
            os.unlink(fname)
        elif type(item) is str:
            if item not in self:
                raise KeyError("ESSID not in storage")
            essid_root, pyrfiles = self.essids[item]
            del self.essids[item]
            for fname in pyrfiles.itervalues():
                os.unlink(fname)
            os.unlink(os.path.join(essid_root, 'essid'))
            os.rmdir(essid_root)

    def containskey(self, essid, key):
        """Return True if the given (ESSID,key) combination is stored."""
        if essid not in self.essids:
            raise KeyError("ESSID not in store.")
        return key in self.essids[essid][1]

    def keycount(self, essid):
        """Returns the number of keys that can currently be used to receive
           results for the given ESSID.
        """
        if essid not in self.essids:
            raise KeyError("ESSID not in store.")
        return len(self.essids[essid][1])

    def iterkeys(self, essid):
        """Iterate over all keys that can be used to receive results."""
        if essid not in self.essids:
            raise KeyError("ESSID not in store.")
        return tuple(self.essids[essid][1]).__iter__()

    def create_essid(self, essid):
        """Create the given ESSID in the storage.

           Re-creating a ESSID is a no-op.
        """
        if len(essid) < 1 or len(essid) > 32:
            raise ValueError("ESSID '%s' invalid." % (essid, ))
        root = os.path.join(self.basepath, hashlib.md5(essid).hexdigest()[:8])
        if not os.path.exists(root):
            os.makedirs(root)
            with open(os.path.join(root, 'essid'), 'wb') as f:
                f.write(essid)
            self.essids[essid] = (root, {})


class FSPasswordStore(PasswordStore):

    def __init__(self, basepath):
        PasswordStore.__init__(self)
        self.basepath = basepath
        if not os.path.exists(self.basepath):
            os.makedirs(self.basepath)
        self.pwfiles = {}
        for pw_h1 in os.listdir(self.basepath):
            if pw_h1 not in PasswordStore.h1_list:
                continue
            pwpath = os.path.join(self.basepath, pw_h1)
            for pwfile in os.listdir(pwpath):
                if pwfile[-3:] != '.pw':
                    continue
                self.pwfiles[pwfile[:len(pwfile)-3]] = pwpath

    def __contains__(self, key):
        """Return True if the given key is currently in the storage."""
        return key in self.pwfiles

    def __iter__(self):
        """Iterate over all keys that can be used to receive password-sets."""
        return self.pwfiles.keys().__iter__()

    def __len__(self):
        """Return the number of keys that can be used to receive
           password-sets.
        """
        return len(self.pwfiles)

    def __getitem__(self, key):
        """Return the collection of passwords indexed by the given key."""
        filename = os.path.join(self.pwfiles[key], key) + '.pw'
        with open(filename, 'rb') as f:
            buf = f.read()
        if buf[:4] == "PAW2":
            inp = PAW2_Buffer()
        elif buf[:4] == "PAWD":
            inp = PAWD_Buffer()
        else:
            raise StorageError("'%s' is not a PasswordFile." % filename)
        inp.unpack(buf)
        if inp.key != key:
            raise StorageError("File doesn't match the key '%s'." % inp.key)
        return inp

    def __delitem__(self, key):
        """Delete workunit from storage"""
        path = self.pwfiles.pop(key)
        filename = os.path.join(path, key) + '.pw'
        os.unlink(filename)

    def size(self, key):
        """Return the number of passwords indexed by the given key."""
        return len(self[key])

    def _flush_bucket(self, pw_h1, bucket):
        if len(bucket) == 0:
            return
        if self.unique_check:
            for key, pwpath in self.pwfiles.iteritems():
                if pwpath.endswith(pw_h1):
                    bucket.difference_update(self[key])
                    if len(bucket) == 0:
                        return
        pwpath = os.path.join(self.basepath, pw_h1)
        if not os.path.exists(pwpath):
            os.makedirs(pwpath)
        key, b = PAW2_Buffer(bucket).pack()
        with open(os.path.join(pwpath, key) + '.pw', 'wb') as f:
            f.write(b)
        self.pwfiles[key] = pwpath


class RPCStorage(Storage):

    def __init__(self, url):
        self.cli = xmlrpclib.ServerProxy(url)
        self.essids = RPCESSIDStore(self.cli)
        self.passwords = RPCPasswordStore(self.cli)

    def getStats(self):
        return self.cli.getStats()


class RPCESSIDStore(ESSIDStore):

    def __init__(self, cli):
        self.cli = cli

    def __getitem__(self, (essid, key)):
        """Receive a iterable of (password,PMK)-tuples stored under
           the given ESSID and key.

           Returns a empty iterable if the key is not stored. Raises KeyError
           if the ESSID is not stored.
        """
        buf = self.cli.essids.getitem(essid, key)
        if buf:
            results = PYR2_Buffer()
            results.unpack(buf.data)
            if results.essid != essid:
                raise StorageError("Invalid ESSID in result-collection")
            return results
        else:
            raise KeyError

    def __setitem__(self, (essid, key), results):
        """Store a iterable of (password,PMK)-tuples under the given
           ESSID and key.
        """
        b = xmlrpclib.Binary(PYR2_Buffer(essid, results).pack())
        self.cli.essids.setitem(essid, key, b)

    def __len__(self):
        """Return the number of ESSIDs currently stored."""
        return self.cli.essids.len()

    def __iter__(self):
        """Iterate over all essids currently stored."""
        return self.cli.essids.essids().__iter__()

    def __contains__(self, essid):
        """Return True if the given ESSID is currently stored."""
        return self.cli.essids.contains(essid)

    def __delitem__(self, essid):
        """Delete the given ESSID and all results from the storage."""
        self.cli.essids.delitem(essid)

    def containskey(self, essid, key):
        """Return True if the given (ESSID,key) combination is stored."""
        return self.cli.essids.containskey(essid, key)

    def keycount(self, essid):
        """Returns the number of keys that can currently be used to receive
           results for the given ESSID.
        """
        return self.cli.essids.keycount(essid)

    def iterkeys(self, essid):
        """Iterate over all keys that can be used to receive results."""
        return self.cli.essids.keys(essid).__iter__()

    def create_essid(self, essid):
        """Create the given ESSID in the storage.

           Re-creating a ESSID is a no-op.
        """
        self.cli.essids.createessid(essid)


class RPCPasswordStore(PasswordStore):

    def __init__(self, cli):
        self.cli = cli

    def __iter__(self):
        """Iterate over all keys that can be used to receive password-sets."""
        return self.cli.passwords.keys().__iter__()

    def __len__(self):
        """Return the number of keys that can be used to receive
           password-sets.
        """
        return self.cli.passwords.len()

    def __contains__(self, key):
        """Return True if the given key is currently in the storage."""
        return self.cli.passwords.contains(key)

    def __getitem__(self, key):
        """Return the collection of passwords indexed by the given key."""
        bucket = PAW2_Buffer()
        bucket.unpack(self.cli.passwords.getitem(key).data)
        if bucket.key != key:
            raise StorageError("File doesn't match the key '%s'." % bucket.key)
        return bucket

    def size(self, key):
        """Return the number of passwords indexed by the given key."""
        return self.cli.passwords.size(key)


class RPCServer(SimpleXMLRPCServer.SimpleXMLRPCServer):

    def __init__(self, storage):
        SimpleXMLRPCServer.SimpleXMLRPCServer.__init__(self, ('', 17934), \
                                                        logRequests=False)
        self.storage = storage
        self.methods = {'getStats': self.getStats, \
                        'passwords.keys': self.passwords_keys, \
                        'passwords.len': self.passwords_len, \
                        'passwords.contains': self.passwords_contains, \
                        'passwords.getitem': self.passwords_getitem, \
                        'passwords.size': self.passwords_size, \
                        'essids.essids': self.essids_essids, \
                        'essids.keys': self.essids_keys, \
                        'essids.len': self.essids_len, \
                        'essids.contains': self.essids_contains, \
                        'essids.getitem': self.essids_getitem, \
                        'essids.setitem': self.essids_setitem, \
                        'essids.delitem': self.essids_delitem, \
                        'essids.keycount': self.essids_keycount, \
                        'essids.containskey': self.essids_containskey, \
                        'essids.createessid': self.essids_create_essid}
        self.register_instance(self)

    def _dispatch(self, method, params):
        if method not in self.methods:
            raise AttributeError
        else:
            return self.methods[method](*params)

    def getStats(self):
        return self.storage.getStats()

    def passwords_keys(self):
        return list(self.storage.passwords)

    def passwords_len(self):
        return len(self.storage.passwords)

    def passwords_contains(self, key):
        return key in self.storage.passwords

    def passwords_getitem(self, key):
        newkey, buf = PAW2_Buffer(self.storage.passwords[key]).pack()
        return xmlrpclib.Binary(buf)

    def passwords_size(self, key):
        return self.storage.passwords.size(key)

    def essids_essids(self):
        return list(self.storage.essids)

    def essids_keys(self, essid):
        return list(self.storage.essids.iterkeys(essid))

    def essids_len(self):
        return len(self.storage.essids)

    def essids_contains(self, essid):
        return essid in self.storage.essids

    def essids_getitem(self, essid, key):
        try:
            results = self.storage.essids[essid, key]
        except KeyError:
            return False
        else:
            buf = PYR2_Buffer(essid, results).pack()
            return xmlrpclib.Binary(buf)

    def essids_setitem(self, essid, key, pyr2_buffer):
        results = PYR2_Buffer()
        results.unpack(pyr2_buffer.data)
        if results.essid != essid:
            raise ValueError("Invalid ESSID in result-collection")
        self.storage.essids[essid, key] = results
        return True

    def essids_delitem(self, essid):
        del self.storage.essids[essid]
        return True

    def essids_keycount(self, essid):
        return self.storage.essids.keycount(essid)

    def essids_containskey(self, essid, key):
        return self.storage.essids.containskey(essid, key)

    def essids_create_essid(self, essid):
        self.storage.essids.create_essid(essid)
        return True


if 'sqlalchemy' in sys.modules:
    metadata = sql.MetaData()

    essids_table = sql.Table('essids', metadata, \
                    sql.Column('essid_id', sql.Integer, primary_key=True),
                    sql.Column('essid', UniversalBinary(32), nullable=False),
                    sql.Column('uid', sql.String(32), unique=True, \
                                nullable=False), \
                    mysql_engine='InnoDB')

    passwords_table = sql.Table('passwords', metadata, \
                    sql.Column('_key', sql.String(32), primary_key=True),
                    sql.Column('h1', sql.String(2), nullable=False, \
                               index=True),
                    sql.Column('numElems', sql.Integer, nullable=False),
                    sql.Column('collection_buffer', UniversalBinary(2**24-1), \
                               nullable=False), \
                    mysql_engine='InnoDB')

    results_table = sql.Table('results', metadata, \
                    sql.Column('essid_id', sql.Integer, \
                               sql.ForeignKey('essids.essid_id'), \
                               primary_key=True),
                    sql.Column('_key', sql.String(32), \
                               sql.ForeignKey('passwords._key'), \
                               primary_key=True, index=True),
                    sql.Column('numElems', sql.Integer, nullable=False),
                    sql.Column('results_buffer', UniversalBinary(2**24 - 1), \
                               nullable=False), \
                    mysql_engine='InnoDB')

    class ESSID_DBObject(object):

        def __init__(self, essid):
            if len(essid) < 1 or len(essid) > 32:
                raise ValueError("ESSID '%s' invalid" % (essid, ))
            self.essid = essid
            self.uid = hashlib.md5(essid).hexdigest()

        def __str__(self):
            return str(self.essid)

    class PAW2_DBObject(object):

        def __init__(self, h1, collection):
            self.h1 = h1
            self.collection = tuple(collection)
            self.numElems = len(self.collection)
            key, collection_buffer = PAW2_Buffer(self.collection).pack()
            self.key, self.collection_buffer = key, collection_buffer

        def __len__(self):
            return self.numElems

        def __iter__(self):
            if not hasattr(self, 'collection'):
                self.collection = PAW2_Buffer()
                self.collection.unpack(self.collection_buffer)
                assert len(self.collection) == self.numElems
            return self.collection.__iter__()

    class PYR2_DBObject(object):

        def __init__(self, essid_obj, key, results):
            self.essid = essid_obj
            self.results = tuple(results)
            self.key = key
            self.pack(results)

        def _unpack(self):
            if not hasattr(self, 'results'):
                self.results = PYR2_Buffer()
                self.results.unpack(self.results_buffer)
                assert len(self.results) == self.numElems

        def pack(self, results):
            self.numElems = len(results)
            self.results_buffer = PYR2_Buffer(str(self.essid), results).pack()

        def __len__(self):
            return self.numElems

        def __iter__(self):
            self._unpack()
            return self.results.__iter__()

        def __getitem__(self, idx):
            self._unpack()
            return self.results[idx]

        def getpmkbuffer(self):
            self._unpack()
            return self.results.getpmkbuffer()

    orm.mapper(ESSID_DBObject, \
               essids_table, \
               properties={'results': orm.relation(PYR2_DBObject, \
                                          backref='essid', \
                                          cascade='all,delete,delete-orphan')})
    orm.mapper(PAW2_DBObject, \
               passwords_table, \
               properties={'results': orm.relation(PYR2_DBObject, \
                                          cascade='all,delete,delete-orphan'),
                           '_key': orm.synonym('key', map_column=True)})
    orm.mapper(PYR2_DBObject, \
               results_table, \
               properties={'_key': orm.synonym('key', map_column=True)})

    class SessionContext(object):
        """A wrapper around classes given by sessionmake to add a
          context-manager.
        """

        def __init__(self, SessionClass):
            self.session = SessionClass()

        def __enter__(self):
            return self.session

        def __exit__(self, type, value, traceback):
            if type is not None:
                self.session.rollback()
            self.session.close()

    class SQLStorage(Storage):

        def __init__(self, url):
            self.engine = sql.create_engine(url, echo=False)
            metadata.create_all(self.engine)
            self.SessionClass = orm.sessionmaker(bind=self.engine)
            self.essids = SQLEssidStore(self.SessionClass)
            self.passwords = SQLPasswordStore(self.SessionClass)

        def getStats(self):
            with SessionContext(self.SessionClass) as session:
                q = session.query(sql.func.sum(PAW2_DBObject.numElems))
                pwtotal = q.one()[0]
                pwtotal = 0 if pwtotal is None else int(pwtotal)
                q = session.query(ESSID_DBObject.essid,
                                  sql.func.sum(PAW2_DBObject.numElems))
                q = q.outerjoin(PYR2_DBObject).outerjoin(PAW2_DBObject)
                q = q.group_by(ESSID_DBObject.essid)
                essid_results = {}
                for essid, pwcount in q:
                    if pwcount is not None:
                        essid_results[str(essid)] = int(pwcount)
                    else:
                        essid_results[str(essid)] = 0
                return (pwtotal, essid_results)

    class SQLEssidStore(ESSIDStore):

        def __init__(self, session_class):
            ESSIDStore.__init__(self)
            self.SessionClass = session_class

        def __contains__(self, essid):
            """Return True if the given ESSID is currently stored."""
            with SessionContext(self.SessionClass) as session:
                q = session.query(ESSID_DBObject)
                return q.filter(ESSID_DBObject.essid == essid).count() == 1

        def __iter__(self):
            """Iterate over all essids currently stored."""
            with SessionContext(self.SessionClass) as session:
                essids = session.query(ESSID_DBObject.essid)
            return (str(c[0]) for c in essids)

        def __len__(self):
            """Return the number of ESSIDs currently stored."""
            with SessionContext(self.SessionClass) as session:
                return session.query(ESSID_DBObject).count()

        def __getitem__(self, (essid, key)):
            """Receive a iterable of (password,PMK)-tuples stored under
               the given ESSID and key.

               Returns a empty iterable if the key is not stored. Raises
               KeyError if the ESSID is not stored.
            """
            with SessionContext(self.SessionClass) as session:
                q = session.query(PYR2_DBObject).join(ESSID_DBObject)
                result = q.filter(sql.and_(ESSID_DBObject.essid == essid, \
                                           PYR2_DBObject.key == key)).first()
                if result is None:
                    raise KeyError("No result for ESSID:Key-combination " \
                                     "(%s:%s)." % (essid, key))
                else:
                    return result

        def __setitem__(self, (essid, key), results):
            """Store a iterable of (password,PMK)-tuples under the given
               ESSID and key.
            """
            with SessionContext(self.SessionClass) as session:
                q = session.query(ESSID_DBObject)
                essid_obj = q.filter(ESSID_DBObject.essid == essid).one()
                session.add(PYR2_DBObject(essid_obj, key, results))
                try:
                    session.commit()
                except sql.exc.IntegrityError:
                    # Assume we hit a concurrent insert that causes
                    # a constraint-error on (essid-key).
                    session.rollback()
                    q = session.query(PYR2_DBObject).join(ESSID_DBObject)
                    q = q.filter(sql.and_( \
                                 ESSID_DBObject.essid == essid_obj.essid, \
                                 PYR2_DBObject.key == key))
                    result_obj = q.one()
                    result_obj.pack(results)
                    session.commit()

        def __delitem__(self, item):
            """Delete the given (ESSID:key)-resultset or the entire ESSID
               and all results from the storage.
            """
            if type(item) is tuple:
                essid, key = item
            else:
                essid = item
                key = None
            with SessionContext(self.SessionClass) as session:
                essid_query = session.query(ESSID_DBObject)
                essid_query = essid_query.filter(ESSID_DBObject.essid == essid)
                essid_obj = essid_query.one()
                res_query = session.query(PYR2_DBObject)
                res_query = res_query.filter(PYR2_DBObject.essid == essid_obj)
                if key is not None:
                    res_query = res_query.filter(PYR2_DBObject.key == key)
                    assert res_query.delete(synchronize_session=False) == 1
                else:
                    res_query.delete(synchronize_session=False)
                    assert essid_query.delete(synchronize_session=False) == 1
                session.commit()

        def containskey(self, essid, key):
            """Return True if the given (ESSID,key) combination is stored."""
            with SessionContext(self.SessionClass) as session:
                q = session.query(PYR2_DBObject).join(ESSID_DBObject)
                q = q.filter(sql.and_(ESSID_DBObject.essid == essid, \
                                      PYR2_DBObject.key == key))
                return q.count() == 1

        def iterkeys(self, essid):
            """Iterate over all keys that can be used to receive
               results.
            """
            with SessionContext(self.SessionClass) as session:
                q = session.query(PAW2_DBObject.key)
                q = q.join(PYR2_DBObject).join(ESSID_DBObject)
                q = q.filter(ESSID_DBObject.essid == essid)
                keys = q.all()
            return (c[0] for c in keys)

        def keycount(self, essid):
            """Returns the number of keys that can currently be used to receive
               results for the given ESSID.
            """
            with SessionContext(self.SessionClass) as session:
                q = session.query(PAW2_DBObject.key)
                q = q.join(PYR2_DBObject).join(ESSID_DBObject)
                q = q.filter(ESSID_DBObject.essid == essid)
                return q.count()

        def create_essid(self, essid):
            """Create the given ESSID in the storage.

               Re-creating a ESSID is a no-op.
            """
            with SessionContext(self.SessionClass) as session:
                essid_obj = ESSID_DBObject(essid)
                session.add(essid_obj)
                session.commit()

    class SQLPasswordStore(PasswordStore):

        def __init__(self, session_class):
            PasswordStore.__init__(self)
            self.SessionClass = session_class

        def __contains__(self, key):
            """Return True if the given key is currently in the storage."""
            with SessionContext(self.SessionClass) as session:
                q = session.query(PAW2_DBObject)
                return q.filter(PAW2_DBObject.key == key).count() == 1

        def __iter__(self):
            """Iterate over all keys that can be used to receive
               password-sets.

               The order of the keys is randomized on every call to __iter__
            """
            with SessionContext(self.SessionClass) as session:
                keys = session.query(PAW2_DBObject.key)
            keys = [c[0] for c in keys]
            random.shuffle(keys)
            return keys.__iter__()

        def __len__(self):
            """Return the number of keys that can be used to receive
           password-sets.
           """
            with SessionContext(self.SessionClass) as session:
                return session.query(PAW2_DBObject).count()

        def __getitem__(self, key):
            """Return the collection of passwords indexed by the given key."""
            with SessionContext(self.SessionClass) as session:
                q = session.query(PAW2_DBObject)
                return q.filter(PAW2_DBObject.key == key).one()
        
        def __delitem__(self, key):
            """Delete the collection of passwords indexed by the given key."""
            with SessionContext(self.SessionClass) as session:
                q = session.query(PAW2_DBObject)
                q = q.filter(PAW2_DBObject.key == key)
                assert q.delete(synchronize_session=False) == 1
                session.commit()

        def size(self, key):
            """Return the number of passwords indexed by the given key."""
            with SessionContext(self.SessionClass) as session:
                q = session.query(PAW2_DBObject.numElems)
                return q.filter(PAW2_DBObject.key == key).one()[0]

        def _flush_bucket(self, pw_h1, bucket):
            if len(bucket) == 0:
                return
            with SessionContext(self.SessionClass) as session:
                if self.unique_check:
                    q = session.query(PAW2_DBObject)
                    for db_bucket in q.filter(PAW2_DBObject.h1 == pw_h1):
                        bucket.difference_update(db_bucket)
                        if len(bucket) == 0:
                            return
                session.add(PAW2_DBObject(pw_h1, bucket))
                session.commit()
