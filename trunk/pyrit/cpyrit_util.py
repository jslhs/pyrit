# -*- coding: UTF-8 -*-
#
#    Copyright 2008, 2009, Lukas Lueg, knabberknusperhaus@yahoo.de
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

"""Various utility- and backend- related classes and data for Pyrit.

   EssidStore and PasswordStore are the primary storage classes. Details of
   their implementation are reasonably well hidden behind the concept of
   key:value interaction.
   
   AsyncFileWriter is used for threaded, buffered output.
   
   genCowpHeader and genCowEntries are used to convert results to cowpatty's
   binary format.
   
   PMK_TESTVECTORS has two ESSIDs and ten password:PMK pairs each to verify
   local installations.
"""

import cStringIO
import hashlib
import itertools
import os
import struct
import threading
import zlib

from _cpyrit._cpyrit_util import genCowpHeader, genCowpEntries
from _cpyrit import VERSION

class AsyncFileWriter(threading.Thread):
    """A buffered, asynchronous file-like object to be wrapped around an already
       opened file handle.
    
       Writing to this object will only block if the internal buffer
       exceeded it's maximum size. The call to .write() is done in a different thread.
    """ 
    def __init__(self, filehndl, maxsize=10*1024**2):
        """Create a instance writing to the given file-like-object and buffering
           maxsize before blocking."""
        threading.Thread.__init__(self)
        self.shallstop = False
        self.hasstopped = False
        self.filehndl = filehndl
        self.maxsize = maxsize
        self.excp = None
        self.buf = cStringIO.StringIO()
        self.cv = threading.Condition()
        self.start()
        
    def close(self):
        """Stop the writer and wait for it to finish.
        
           It is the caller's responsibility to close the file handle that was
           used for initialization. Exceptions in the writer-thread are
           not re-raised.
        """
        self.cv.acquire()
        try:
            self.shallstop = True
            self.cv.notifyAll()
            while not self.hasstopped:
                self.cv.wait()
            self._raise()
        finally:
            self.cv.release()

    def write(self, data):
        """Write data to the buffer, block if necessary.
        
           Exceptions in the writer-thread are re-raised in the caller's thread
           before the data is written.
        """
        self.cv.acquire()
        try:
            self._raise()
            while self.buf.tell() > self.maxsize:
                self.cv.wait()
                if self.shallstop:
                    raise IOError, "Writer has already been closed."
            self.buf.write(data)
            self.cv.notifyAll()
        finally:
            self.cv.release()
            
    def closeAsync(self):
        """Signal the writer to stop and return to caller immediately.
        
           It is the caller's responsibility to close the file handle that was
           used for initialization. Exceptions are not re-raised.
        """
        self.cv.acquire()
        try:
            self.shallstop = True
            self.cv.notifyAll()
        finally:
            self.cv.release()
    
    def join(self):
        """Wait for the writer to stop.
        
           Exceptions in the writer-thread are re-raised in the caller's thread
           after writer has stopped.
        """
        self.cv.acquire()
        try:
            while not self.hasstopped:
                self.cv.wait()
            self._raise()
        finally:
            self.cv.release()

    def _raise(self):
        # Assumes we hold self.cv
        if self.excp:
            e = self.excp
            self.excp = None
            self.shallstop = True
            self.cv.notifyAll()
            raise e

    def run(self):
        try:
            while True:
                self.cv.acquire()
                try:
                    data = None
                    if self.buf.tell() == 0:
                        if self.shallstop:
                            break
                        else:
                            self.cv.wait()
                    else:
                        data = self.buf.getvalue()
                        self.buf = cStringIO.StringIO()
                        self.cv.notifyAll()
                finally:
                    self.cv.release()
                if data:
                    self.filehndl.write(data)
            self.filehndl.flush()
        except Exception, e:
            self.excp = type(e)(str(e)) # Re-create a 'trans-thread-safe' instance
        finally:
            self.cv.acquire()
            self.shallstop = self.hasstopped = True
            self.cv.notifyAll()
            self.cv.release()


class EssidStore(object):
    """Storage-class responsible for ESSID and PMKs.
    
       Callers should use the default-iterator to cycle over available ESSIDs.
       Results are returned as dictionaries and indexed by keys. The Keys may be
       received from .iterkeys() or from other storage sources (PasswordStore).
    """
    _pyr_preheadfmt = '<4sH'
    _pyr_preheadfmt_size = struct.calcsize(_pyr_preheadfmt)
    def __init__(self, basepath):
        self.basepath = basepath
        if not os.path.exists(self.basepath):
            os.makedirs(self.basepath)
        self.asyncwriters = []
        self.essidrootcache = {}

    def _getessidroot(self, essid):
        return self.essidrootcache.setdefault(essid, os.path.join(self.basepath, hashlib.md5(essid).hexdigest()[:8]))

    def _syncwriters(self):
        while len(self.asyncwriters) > 0:
            essid, key, f, writer = self.asyncwriters.pop()
            writer.join()
            f.close()

    def __getitem__(self, (essid, key)):
        """Receive a tuple of (password,PMK)-tuples stored under
           the given ESSID and key.
        """
        if not self.containskey(essid, key):
            return ()
        filename = os.path.join(self._getessidroot(essid), key) + '.pyr'
        f = open(filename, 'rb')
        buf = f.read()
        f.close()
        md = hashlib.md5()
        magic, essidlen = struct.unpack(EssidStore._pyr_preheadfmt, buf[:EssidStore._pyr_preheadfmt_size])
        if magic == 'PYR2':
            headfmt = "<%ssi%ss" % (essidlen, md.digest_size)
            headsize = struct.calcsize(headfmt)
            file_essid, numElems, digest = struct.unpack(headfmt, buf[EssidStore._pyr_preheadfmt_size:EssidStore._pyr_preheadfmt_size+headsize])
            if file_essid != essid:
                raise IOError, "ESSID in result-file mismatches."
            pmkoffset = EssidStore._pyr_preheadfmt_size + headsize
            pwoffset = pmkoffset + numElems * 32
            md.update(file_essid)
            md.update(buf[pmkoffset:])
            if md.digest() != digest:
                raise IOError, "Digest check failed on result-file '%s'." % filename
            results = tuple(zip(zlib.decompress(buf[pwoffset:]).split('\n'),
                          [buf[pmkoffset + i*32:pmkoffset + i*32 + 32] for i in xrange(numElems)]))
        else:
            raise IOError, "Not a PYR2-file."
        if len(results) != numElems:
            raise IOError, "Header announced %i results but %i unpacked" % (numElems, len(results))
        return results
    
    def __setitem__(self, (essid, key), results):
        """Store a iterable of password:PMK tuples under the given ESSID and key."""
        essidpath = self._getessidroot(essid)
        if not os.path.exists(essidpath):
            raise KeyError, "ESSID not in store."
        filename = os.path.join(essidpath, key) + '.pyr'
        pws, pmks = zip(*results)
        pwbuffer = zlib.compress('\n'.join(pws), 1)
        if hashlib.md5(pwbuffer).hexdigest() != key:
            raise ValueError, "Results and key mismatch."
        pmkbuffer = ''.join(pmks)
        md = hashlib.md5()
        md.update(essid)
        md.update(pmkbuffer)
        md.update(pwbuffer)        
        f = open(filename, 'wb')
        writer = AsyncFileWriter(f)
        try:
            writer.write(struct.pack('<4sH%ssi%ss' % (len(essid), md.digest_size), 'PYR2', len(essid), essid, len(pws), md.digest()))
            writer.write(pmkbuffer)
            writer.write(pwbuffer)
        finally:
            writer.closeAsync()
        self.asyncwriters.append((essid, key, f, writer))
        for essid, key, f, writer in self.asyncwriters:
            if not writer.isAlive():
                f.close()
                self.asyncwriters.remove((essid, key, f, writer))
        
    def __len__(self):
        return len([x for x in self])

    def __iter__(self):
        essids = set()
        for essid_hash in os.listdir(self.basepath):
            f = open(os.path.join(self.basepath, essid_hash, 'essid'), 'rb')
            essid = f.read()
            f.close()
            if essid_hash == hashlib.md5(essid).hexdigest()[:8]:
                essids.add(essid)
            else:
                print >>sys.stderr, "ESSID %s seems to be corrupted." % essid_hash
        return sorted(essids).__iter__()
            
    def __contains__(self, essid):
        """Return True if the given ESSID is currently stored."""
        essid_root = self._getessidroot(essid)
        if os.path.exists(essid_root):
            f = open(os.path.join(essid_root, 'essid'), 'rb')
            e = f.read()
            f.close()
            return e == essid
        else:
            return False

    def containskey(self, essid, key):
        """Return True if the given ESSID:key combination is stored.""" 
        for newessid, newkey, f, writer in self.asyncwriters:
            if newkey == key and newessid == essid:
                # We assume that the file will make it to the disk.
                return True
        essidpath = self._getessidroot(essid)
        if not os.path.exists(essidpath):
            raise KeyError, "ESSID not in store."
        filename = os.path.join(essidpath, key) + '.pyr'
        return os.path.exists(filename)

    def iterkeys(self, essid):
        """Iterate over all keys that can currently be used to receive results
           for the given ESSID.
        """
        self._syncwriters()
        essidpath = self._getessidroot(essid)
        if not os.path.exists(essidpath):
            raise KeyError, "ESSID not in store."
        keys = set()
        for pyrfile in os.listdir(essidpath):
            if pyrfile[-4:] != '.pyr':
                continue
            keys.add(pyrfile[:len(pyrfile)-4])
        return keys.__iter__()
        
    def iterresults(self, essid):
        """Iterate over all results currently stored for the given ESSID."""
        for key in self.iterkeys(essid):
            yield self[essid, key]

    def iteritems(self, essid):
        """Iterate over all keys and results currently stored for the given ESSID."""
        for key in self.iterkeys(essid):
            yield (key, self[essid, key])

    def create_essid(self, essid):
        """Create the given ESSID in the storage.
        
           Re-creating a ESSID is a no-op.
        """
        if len(essid) < 3 or len(essid) > 32:
            raise ValueError, "ESSID invalid."
        essid_root = self._getessidroot(essid)
        if not os.path.exists(essid_root):
            os.makedirs(essid_root)
        f = open(os.path.join(essid_root, 'essid'), 'wb')
        f.write(essid)
        f.close()

    def delete_essid(self, essid):
        """Delete the given ESSID and all results from the storage."""
        if essid not in self:
            raise KeyError, "ESSID not in store."
        essid_root = self._getessidroot(essid)
        for fname in os.listdir(essid_root):
            if fname[-4:] == '.pyr':
                os.unlink(os.path.join(essid_root, fname))
        os.unlink(os.path.join(essid_root, 'essid'))
        os.rmdir(essid_root)


class PasswordStore(object):
    """Storage-class responsible for passwords.
    
       Passwords can be received by key and are returned as lists.
       The iterator cycles over all available keys.
    """
    h1_list = ["%02.2X" % i for i in xrange(256)]
    del i
    def __init__(self, basepath):
        self.basepath = basepath
        if not os.path.exists(self.basepath):
            os.makedirs(self.basepath)
        self.pwbuffer = {}
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
        """Returns True if the given key is currently in the storage."""
        return key in self.pwfiles

    def __iter__(self):
        """Iterate over all keys that can be used to receive password-sets."""
        return self.pwfiles.__iter__()

    def __getitem__(self, key):
        """Returns the collection of passwords indexed by the given key.""" 
        filename = os.path.join(self.pwfiles[key], key) + '.pw'
        f = open(filename, 'rb')
        buf = f.read()
        f.close()
        if buf[:4] == "PAW2":
            md = hashlib.md5()
            md.update(buf[4+md.digest_size:])
            if md.digest() != buf[4:4+md.digest_size]:
                raise IOError, "Digest check failed for %s" % filename
            if md.hexdigest() != key:
                raise IOError, "File '%s' doesn't match the key '%s'." % (filename, md.hexdigest())
            return tuple(zlib.decompress(buf[4+md.digest_size:]).split('\n'))
        else:
            raise IOError, "'%s' is not a PasswordFile." % filename

    def _flush_bucket(self, pw_h1, bucket):
        if len(bucket) == 0:
            return
        for key, pwpath in self.pwfiles.iteritems():
            if pwpath.endswith(pw_h1):
                bucket.difference_update(self[key])
                if len(bucket) == 0:
                    return
        pwpath = os.path.join(self.basepath, pw_h1)
        if not os.path.exists(pwpath):
            os.makedirs(pwpath)
        md = hashlib.md5()
        b = zlib.compress('\n'.join(sorted(bucket)), 1)
        md.update(b)
        key = md.hexdigest()
        f = open(os.path.join(pwpath, key) + '.pw', 'wb')
        f.write('PAW2')
        f.write(md.digest())
        f.write(b)
        f.close()
        self.pwfiles[key] = pwpath

    def iterkeys(self):
        """Iterate over all keys that can be used to receive password-sets."""
        return self.__iter__()
            
    def iterpasswords(self):
        """Iterate over all available passwords-sets."""
        for key in self:
            yield self[key]

    def iteritems(self):
        """Iterate over all keys and password-sets."""
        for key in self:
            yield (key, self[key])

    def flush_buffer(self):
        """Flush all passwords currently buffered to the storage.
           
           For efficiency reasons this function should not be called if the
           caller wants to add more passwords in the foreseeable future.
        """
        for pw_h1, pw_bucket in self.pwbuffer.iteritems():
            self._flush_bucket(pw_h1, pw_bucket)
            self.pwbuffer[pw_h1] = set()

    def store_password(self, passwd):
        """Add the given password to storage. The implementation ensures that
           passwords remain unique over the entire storage.
           
           Passwords passed to this function are buffered in memory for better
           performance and efficiency. It is the caller's responsibility to
           call .flush_buffer() when he is done.
        """
        passwd = passwd.strip()
        if len(passwd) < 8 or len(passwd) > 63:
            return
        pw_h1 = PasswordStore.h1_list[hash(passwd) & 0xFF]
        pw_bucket = self.pwbuffer.setdefault(pw_h1, set())
        pw_bucket.add(passwd)
        if len(pw_bucket) >= 20000:
            self._flush_bucket(pw_h1, pw_bucket)
            self.pwbuffer[pw_h1] = set()


PMK_TESTVECTORS = {
    'foo': {
        'soZcEvntHVrGRDIxNaBCyUL': (247,210,173,42,68,187,144,253,145,93,126,250,16,188,100,55,89,153,135,155,198,86,124,33,45,16,9,54,113,194,159,211),
        'EVuYtpQCAZzBXyWNRGTI': (5,48,168,39,10,98,151,201,8,80,23,138,19,24,24,50,66,214,189,180,159,97,194,27,212,124,114,100,253,62,50,170),
        'XNuwoiGMnjlkxBHfhyRgZrJItFDqQVESm': (248,208,207,115,247,35,170,203,214,228,228,21,40,214,165,0,98,194,136,62,110,253,69,205,67,215,119,109,72,226,255,199),
        'bdzPWNTaIol': (228,236,73,0,189,244,21,141,84,247,3,144,2,164,99,205,37,72,218,202,182,246,227,84,24,58,147,114,206,221,40,127),
        'nwUaVYhRbvsH': (137,21,14,210,213,68,210,123,35,143,108,57,196,47,62,161,150,35,165,197,154,61,76,14,212,88,125,234,51,38,159,208),
        'gfeuvPBbaDrQHldZzRtXykjFWwAhS': (88,127,99,35,137,177,147,161,244,32,197,233,178,1,96,247,5,109,163,250,35,222,188,143,155,70,106,1,253,79,109,135),
        'QcbpRkAJerVqHz': (158,124,37,190,197,150,225,165,3,34,104,147,107,253,233,127,33,239,75,11,169,187,127,171,187,165,166,187,95,107,137,212),
        'EbYJsCNiwXDmHtgkFVacuOv': (136,5,34,189,145,60,145,54,179,198,195,223,34,180,144,3,116,102,39,134,68,82,210,185,190,199,36,25,136,152,0,111),
        'GpIMrFZwLcqyt': (28,144,175,10,200,46,253,227,219,35,98,208,220,11,101,95,62,244,80,221,111,49,206,255,174,100,240,240,33,229,172,207),
        'tKxgswlaOMLeZVScGDW': (237,62,117,60,38,107,65,166,113,174,196,221,128,227,69,89,23,77,119,234,41,176,145,105,92,40,157,151,229,50,81,65)
        },
    'bar': {
        'zLwSfveNskZoR': (38,93,196,77,112,65,163,197,249,158,180,107,231,140,188,60,254,77,12,210,77,185,233,59,79,212,222,181,44,19,127,220),
        'lxsvOCeZXop': (91,39,98,36,82,2,162,106,12,244,4,113,155,120,131,133,11,209,12,12,240,213,203,156,129,148,28,64,31,61,162,13),
        'tfHrgLLOA': (110,72,123,80,222,233,150,54,40,99,205,155,177,157,174,172,87,11,247,164,87,85,136,165,21,107,93,212,71,133,145,211),
        'vBgsaSJrlqajUlQJM': (113,110,180,150,204,221,61,202,238,142,147,118,177,196,65,79,102,47,179,80,175,95,251,35,227,220,47,121,50,125,55,16),
        'daDIHwIMKSUaKWXS': (33,87,211,99,26,70,123,19,254,229,148,97,252,182,3,44,228,125,85,141,247,223,166,133,246,37,204,145,100,218,66,70),
        'agHOeAjOpK': (226,163,62,215,250,63,6,32,130,34,117,116,189,178,245,172,74,26,138,10,106,119,15,214,210,114,51,94,254,57,81,200),
        'vRfEagJIzSohxsakj': (61,71,159,35,233,27,138,30,228,121,38,201,57,83,192,211,248,207,149,12,147,70,190,216,52,14,165,190,226,180,62,210),
        'PuDomzkiwsejblaXs': (227,164,137,231,16,31,222,169,134,1,238,190,55,126,255,88,178,118,148,119,244,130,183,219,124,249,194,96,94,159,163,185),
        'RErvpNrOsW': (24,145,197,137,14,154,1,36,73,148,9,192,138,157,164,81,47,184,41,75,225,34,71,153,59,253,127,179,242,193,246,177),
        'ipptbpKkCCep': (81,34,253,39,124,19,234,163,32,10,104,88,249,29,40,142,24,173,1,68,187,212,21,189,74,88,83,228,7,100,23,244)
        }
    }
for essid in PMK_TESTVECTORS:
    for pw in PMK_TESTVECTORS[essid]:
        PMK_TESTVECTORS[essid][pw] = ''.join(map(chr, PMK_TESTVECTORS[essid][pw]))
del essid
del pw

