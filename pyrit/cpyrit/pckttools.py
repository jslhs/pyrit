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

""" This modules deals with parsing of IEEE802.11-packets and attacking
    EAPOL-authentications.

    Scapy's Packet-class is extended with some utility-functions as described.

    The class PacketParser can be used to analyze a (possibly gzip-compressed)
    packet-capture-file in pcap-format. The representation gained from it is
    not exact in the strictest sense but a straightforward hierarchy of
    AccessPoint -> Station -> EAPOLAuthentication.
"""

from __future__ import with_statement

import tempfile
import threading
import Queue

import util
import _cpyrit_cpu

try:
    import scapy.config
    scapy.config.conf.logLevel = 40 # Suppress useless warnings from scapy...
    import scapy.fields
    import scapy.layers.dot11
    import scapy.packet
    import scapy.utils
except ImportError, e:
    raise util.ScapyImportError(e)

scapy.config.Conf.l2types.register_num2layer(119,
                                            scapy.layers.dot11.PrismHeader)


def isFlagSet(self, name, value):
    """Return True if the given field 'includes' the given value.
       Exact behaviour of this function is specific to the field-type.
    """
    field, val = self.getfield_and_val(name)
    if isinstance(field, scapy.fields.EnumField):
        if val not in field.i2s:
            return False
        return field.i2s[val] == value
    else:
        return (1 << field.names.index([value])) & self.__getattr__(name) != 0
scapy.packet.Packet.isFlagSet = isFlagSet
del isFlagSet


def areFlagsSet(self, name, values):
    """Return True if the given field 'includes' all of the given values."""
    return all(self.isFlagSet(name, value) for value in values)
scapy.packet.Packet.areFlagsSet = areFlagsSet
del areFlagsSet


def areFlagsNotSet(self, name, values):
    """Return True if the given field 'includes' none of the given values."""
    return all(not self.isFlagSet(name, value) for value in values)
scapy.packet.Packet.areFlagsNotSet = areFlagsNotSet
del areFlagsNotSet


def iterSubPackets(self, cls):
    """Iterate over all layers of the given type in packet 'self'."""
    if cls not in self:
        return
    elt = self[cls]
    while elt:
        yield elt
        elt = elt[cls:2]
scapy.packet.Packet.iterSubPackets = iterSubPackets
del iterSubPackets


class XStrFixedLenField(scapy.fields.StrFixedLenField):
    """String-Field with nice repr() for hexdecimal strings"""

    def i2repr(self, pkt, x):
        return util.str2hex(scapy.fields.StrFixedLenField.i2m(self, pkt, x))


class XStrLenField(scapy.fields.StrLenField):
    """String-Field of variable size with nice repr() for hexdecimal strings"""

    def i2repr(self, pkt, x):
        return util.str2hex(scapy.fields.StrLenField.i2m(self, pkt, x))


class EAPOL_Key(scapy.packet.Packet):
    """EAPOL Key frame"""
    name = "EAPOL Key"
    fields_desc = [scapy.fields.ByteEnumField("DescType", 254,
                                                {2: "RSN Key",
                                                254: "WPA Key"})]
scapy.packet.bind_layers(scapy.layers.l2.EAPOL, EAPOL_Key, type=3)


class EAPOL_AbstractEAPOLKey(scapy.packet.Packet):
    """Base-class for EAPOL WPA/RSN-Key frames"""
    fields_desc = [scapy.fields.FlagsField("KeyInfo", 0, 16,
                                ["HMAC_MD5_RC4", "HMAC_SHA1_AES", "undefined",\
                                 "pairwise", "idx1", "idx2", "install",\
                                 "ack", "mic", "secure", "error", "request", \
                                 "encrypted"]),
        scapy.fields.ShortField("KeyLength", 0),
        scapy.fields.LongField("ReplayCounter", 0),
        XStrFixedLenField("Nonce", '\x00' * 32, 32),
        XStrFixedLenField("KeyIV", '\x00' * 16, 16),
        XStrFixedLenField("WPAKeyRSC", '\x00' * 8, 8),
        XStrFixedLenField("WPAKeyID", '\x00' * 8, 8),
        XStrFixedLenField("WPAKeyMIC", '\x00' * 16, 16),
        scapy.fields.ShortField("WPAKeyLength", 0),
        scapy.fields.ConditionalField(
                            XStrLenField("WPAKey", None,
                                length_from=lambda pkt: pkt.WPAKeyLength), \
                            lambda pkt: pkt.WPAKeyLength > 0)]


class EAPOL_WPAKey(EAPOL_AbstractEAPOLKey):
    name = "EAPOL WPA Key"
    keyscheme = 'HMAC_MD5_RC4'
scapy.packet.bind_layers(EAPOL_Key, EAPOL_WPAKey, DescType=254)


class EAPOL_RSNKey(EAPOL_AbstractEAPOLKey):
    name = "EAPOL RSN Key"
    keyscheme = 'HMAC_SHA1_AES'
scapy.packet.bind_layers(EAPOL_Key, EAPOL_RSNKey, DescType=2)


class AccessPoint(object):

    def __init__(self, mac):
        self.mac = mac
        self.essidframe = None
        self.essid = None
        self.stations = {}

    def __iter__(self):
        return self.stations.values().__iter__()

    def __str__(self):
        return self.mac

    def __contains__(self, mac):
        return mac in self.stations

    def __getitem__(self, mac):
        return self.stations[mac]

    def __setitem__(self, mac, station):
        self.stations[mac] = station

    def __len__(self):
        return len(self.stations)

    def getCompletedAuthentications(self):
        """Return list of completed Authentication."""
        auths = []
        for station in self.stations.itervalues():
            auths.extend(station.getAuthentications())
        return auths

    def isCompleted(self):
        """Returns True if this instance includes at least one valid
           authentication.
        """
        return any(station.isCompleted() for station in self)


class Station(object):

    def __init__(self, mac, ap):
        self.ap = ap
        self.mac = mac
        self.frames = {}

    def __str__(self):
        return self.mac

    def __iter__(self):
        return self.getAuthentications().__iter__()

    def __len__(self):
        return len(self.auths)

    def addAuthenticationFrame(self, idx, pckt_idx, pckt):
        if idx == 0:
            return self.addChallengeFrame(pckt_idx, pckt)
        elif idx == 1:
            return self.addResponseFrame(pckt_idx, pckt)
        elif idx == 2:
            return self.addConfirmationFrame(pckt_idx, pckt)
        else:
            raise IndexError("Invalid authentication-phase.")

    def addChallengeFrame(self, pckt_idx, pckt):
        """Store a packet that contains the EAPOL-challenge"""
        frames = self.frames.setdefault(pckt.ReplayCounter, ({}, {}, {}))
        if pckt.Nonce not in frames[0]:
            frames[0][pckt.Nonce] = (pckt_idx, pckt)
            return self._buildAuthentications({pckt.Nonce: (pckt_idx, pckt)}, \
                                              frames[1], frames[2])

    def addResponseFrame(self, pckt_idx, pckt):
        """Store a packet that contains the EAPOL-response"""
        frames = self.frames.setdefault(pckt.ReplayCounter, ({}, {}, {}))

        if EAPOL_WPAKey in pckt:
            keypckt = pckt[EAPOL_WPAKey]
        elif EAPOL_RSNKey in pckt:
            keypckt = pckt[EAPOL_RSNKey]
        else:
            raise TypeError("No key-packet in frame")

        # WPAKeys 'should' set HMAC_MD5_RC4, RSNKeys HMAC_SHA1_AES
        # However we've seen cases where a WPAKey-packet sets
        # HMAC_SHA1_AES in it's KeyInfo-field (see issue #111)
        if keypckt.isFlagSet('KeyInfo', EAPOL_WPAKey.keyscheme):
            version = EAPOL_WPAKey.keyscheme
        elif keypckt.isFlagSet('KeyInfo', EAPOL_RSNKey.keyscheme):
            version = EAPOL_RSNKey.keyscheme
        else:
            # Fallback to packet-types's own default, in case the
            # KeyScheme is never set. Should not happen...
            version = keypckt.keyscheme

        # We need a revirginized version of the EAPOL-frame which produced
        # that MIC.
        keymic_frame = pckt[scapy.layers.dot11.EAPOL].copy()
        keymic_frame.WPAKeyMIC = '\x00' * len(keymic_frame.WPAKeyMIC)
        # Strip padding and cruft from frame
        keymic_frame = str(keymic_frame)[:keymic_frame.len + 4]

        response = (version, keypckt.Nonce, keymic_frame, keypckt.WPAKeyMIC)
        if response not in frames[1]:
            frames[1][response] = (pckt_idx, pckt)
            return self._buildAuthentications(frames[0], \
                                              {response: (pckt_idx, pckt)}, \
                                              frames[2])
    
    def addConfirmationFrame(self, pckt_idx, pckt):
        """Store a packet that contains the EAPOL-confirmation"""
        frames = self.frames.setdefault(pckt.ReplayCounter - 1, ({}, {}, {}))
        if pckt.Nonce not in frames[2]:
            frames[2][pckt.Nonce] = (pckt_idx, pckt)
            return self._buildAuthentications(frames[0], frames[1], \
                                              {pckt.Nonce: (pckt_idx, pckt)})
    
    def _buildAuthentications(self, f1_frames, f2_frames, f3_frames):
        auths = []
        for (version, snonce, keymic_frame, WPAKeyMIC), \
          (f2_idx, f2) in f2_frames.iteritems():
            # Combinations with Frame3 are of higher value as the AP
            # acknowledges that the STA used the correct PMK in Frame2
            for anonce, (f3_idx, f3) in f3_frames.iteritems():
                if anonce in f1_frames:
                    # We have F1+F2+F3. Frame2 is only cornered by the
                    # ReplayCounter. Technically we don't benefit
                    # from this combination any more than just
                    # F2+F3 but this is the best we can get.
                    f1_idx, f1 = f1_frames[anonce]
                    spread = max(abs(f3_idx - f2_idx), \
                                 abs(f1_idx - f2_idx))
                    auth = EAPOLAuthentication(self, version, snonce, \
                                        anonce, WPAKeyMIC, keymic_frame, \
                                        0, spread, (f1, f2, f3))
                else:
                    # There are no matching first-frames. That's OK.
                    spread = abs(f3_idx - f2_idx)
                    auth = EAPOLAuthentication(self, version, snonce, \
                                        anonce, WPAKeyMIC, keymic_frame, \
                                        1, spread, (None, f2, f3))
                auths.append(auth)
            for anonce, (f1_idx, f1) in f1_frames.iteritems():
                # No third frame. Combinations with Frame1 are possible but
                # can also be triggered by STAs that use an incorrect PMK.
                spread = abs(f1_idx - f2_idx)
                if anonce not in f3_frames:
                    auth = EAPOLAuthentication(self, version, snonce, \
                                        anonce, WPAKeyMIC, keymic_frame, \
                                        2, spread, (f1, f2, None))
                    auths.append(auth)
        return auths

    def getAuthentications(self):
        """Reconstruct a  list of EAPOLAuthentications from captured
           handshake-packets. Best matches come first.
        """
        auths = []
        for frames in self.frames.itervalues():
            auths.extend(self._buildAuthentications(*frames))
        return sorted(auths) 

    def isCompleted(self):
        """Returns True if this instance includes at least one valid
           authentication.
        """
        return len(self.getAuthentications()) > 0


class EAPOLAuthentication(object):

    def __init__(self, station, version, snonce, anonce, keymic, \
                    keymic_frame, quality, spread, frames=None):
        self.station = station
        self.version = version
        self.snonce = snonce
        self.anonce = anonce
        self.keymic = keymic
        self.keymic_frame = keymic_frame
        self.quality = quality
        self.spread = spread
        self.frames = frames

    def getpke(self):
        pke = "Pairwise key expansion\x00" \
               + ''.join(sorted((scapy.utils.mac2str(self.station.ap.mac), \
                                 scapy.utils.mac2str(self.station.mac)))) \
               + ''.join(sorted((self.snonce, self.anonce))) \
               + '\x00'
        return pke
    pke = property(getpke)

    def __lt__(self, other):
        if isinstance(other, EAPOLAuthentication):
            return (self.quality, self.spread) < (other.quality, other.spread)
        else:
            return self < other
            
    def __gt__(self, other):
        return not self < other

    def __str__(self):
        quality = ['good', 'workable', 'bad'][self.quality]
        return "%s, %s, spread %s" % (self.version, quality, self.spread)


class Dot11PacketWriter(object):

    def __init__(self, pcapfile):
        self.writer = scapy.utils.PcapWriter(pcapfile, linktype=105,
                                        gz=pcapfile.endswith('.gz'), sync=True)
        self.pcktcount = 0

    def write(self, pckt):
        if not scapy.layers.dot11.Dot11 in pckt:
            raise RuntimeError("No Dot11-frame in packet.")
        self.writer.write(pckt[scapy.layers.dot11.Dot11])
        self.pcktcount += 1

    def close(self):
        self.writer.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()


class PcapReader(_cpyrit_cpu.PcapReader):
    """Read packets from a 'savefile' or a device using libpcap."""

    def __init__(self, fname=None):
        # Underlying PcapReader is bpf-filtered by default (if possible)
        _cpyrit_cpu.PcapReader.__init__(self, True)
        if fname:
            self.open_offline(fname)
            
    def _set_datalink_handler(self):
        try:
            self.datalink_handler = scapy.config.conf.l2types[self.datalink]
        except KeyError:
            raise ValueError("Datalink-type %i not supported by Scapy" % \
                            self.datalink)

    def open_live(self, device_name):
        """Open a device for capturing packets"""
        _cpyrit_cpu.PcapReader.open_live(self, device_name)
        self._set_datalink_handler()

    def open_offline(self, fname):
        """Open a pcap-savefile"""
        if fname.endswith('.gz'):
            tfile = tempfile.NamedTemporaryFile()
            try:
                with util.FileWrapper(fname) as infile:
                    while True:
                        buf = infile.read(1024**2)
                        if not buf:
                            break
                        tfile.write(buf)
                tfile.flush()
                _cpyrit_cpu.PcapReader.open_offline(self, tfile.name)
            finally:
                tfile.close()
        else:
            _cpyrit_cpu.PcapReader.open_offline(self, fname)
        self._set_datalink_handler()

    def read(self):
        """Read one packet from the capture-source."""
        r = _cpyrit_cpu.PcapReader.read(self)
        if r is not None:
            ts, pckt_string = r
            pckt = self.datalink_handler(pckt_string)
            return pckt
        else:
            return None

    def __iter__(self):
        return self

    def next(self):
        pckt = self.read()
        if pckt is not None:
            return pckt
        else:
            raise StopIteration

    def __enter__(self):
        if self.type is None:
            raise RuntimeError("No device/file opened yet")
        return self
    
    def __exit__(self, type, value, traceback):
        self.close()


class PacketParser(object):
    """Parse packets from a capture-source and reconstruct AccessPoints,
       Stations and EAPOLAuthentications from the data.
    """

    def __init__(self, pcapfile=None, new_ap_callback=None,
                new_station_callback=None, new_keypckt_callback=None,
                new_auth_callback=None):
        self.air = {}
        self.pcktcount = 0
        self.dot11_pcktcount = 0
        self.new_ap_callback = new_ap_callback
        self.new_station_callback = new_station_callback
        self.new_keypckt_callback = new_keypckt_callback
        self.new_auth_callback = new_auth_callback
        if pcapfile is not None:
            self.parse_file(pcapfile)

    def _find_ssid(self, pckt):
        for elt_pckt in pckt.iterSubPackets(scapy.layers.dot11.Dot11Elt):
            if elt_pckt.isFlagSet('ID', 'SSID') \
             and len(elt_pckt.info) == elt_pckt.len \
             and not all(c == '\x00' for c in elt_pckt.info):
                return elt_pckt.info

    def _add_ap(self, ap_mac, pckt):
        ap = self.air.setdefault(ap_mac, AccessPoint(ap_mac))
        if ap.essid is None:
            essid = self._find_ssid(pckt)
            if essid is not None:
                ap.essid = essid
                ap.essidframe = pckt.copy()
                if self.new_ap_callback is not None:
                    self.new_ap_callback(ap)

    def _add_station(self, ap, sta_mac):
        if sta_mac not in ap:
            sta = Station(sta_mac, ap)
            ap[sta_mac] = sta
            if self.new_station_callback is not None:
                self.new_station_callback(sta)

    def _add_keypckt(self, station, idx, pckt):
        new_auths = station.addAuthenticationFrame(idx, self.pcktcount, pckt)
        if self.new_keypckt_callback is not None:
            self.new_keypckt_callback((station, idx, pckt))
        if new_auths is not None and self.new_auth_callback is not None:
            for auth in new_auths:
                self.new_auth_callback((station, auth))    

    def parse_file(self, pcapfile):
        with PcapReader(pcapfile) as rdr:
            self.parse_pcapreader(rdr)

    def parse_pcapreader(self, reader):
        """Parse all packets from a instance of PcapReader.
           
           This method is very fast as it updates PcapReader's BPF-filter
           to exclude unwanted packets from Stations once we are aware of
           their presence.
        """

        def _update_filter(reader, stations, callback, sta):
            if reader.filtered:
                stations.add(sta.mac)
                macs = " or ".join(stations)
                # Once a station is known, we exclude encrypted data-traffic
                # and other unwanted packets
                bpf_string = "not type ctl" \
                             " and not (wlan addr1 %s or wlan addr2 %s)" \
                             " or subtype beacon or subtype probe-resp or" \
                             " subtype assoc-req or (type data and" \
                             " wlan[1] & 0x40 = 0 and not subtype null)" % (macs, macs)
                reader.filter(bpf_string)
            if callback is not None:
                callback(sta)

        if not isinstance(reader, PcapReader):
            raise TypeError("Argument should be of type PcapReader")
        filtered_stations = set()
        old_callback = self.new_station_callback
        self.new_station_callback = \
            lambda sta: _update_filter(reader, filtered_stations, \
                                       old_callback, sta)
        for pckt in reader:
            self.parse_packet(pckt)
        
    def parse_packet(self, pckt):
        """Parse one packet"""
        
        self.pcktcount += 1
        if not scapy.layers.dot11.Dot11 in pckt:
            return
        dot11_pckt = pckt[scapy.layers.dot11.Dot11]
        self.dot11_pcktcount += 1

        if dot11_pckt.isFlagSet('type', 'Control'):
            return

        # Get a AP and a ESSID from a Beacon
        if scapy.layers.dot11.Dot11Beacon in dot11_pckt:
            self._add_ap(dot11_pckt.addr2, dot11_pckt)
            return

        # Get a AP and it's ESSID from a AssociationRequest
        if scapy.layers.dot11.Dot11AssoReq in dot11_pckt:
            self._add_ap(dot11_pckt.addr1, dot11_pckt)

        # Get a AP and it's ESSID from a ProbeResponse
        if scapy.layers.dot11.Dot11ProbeResp in dot11_pckt:
            self._add_ap(dot11_pckt.addr2, dot11_pckt)

        # From now on we are only interested in unicast packets
        if dot11_pckt.isFlagSet('FCfield', 'to-DS') \
         and not int(dot11_pckt.addr2[1], 16) & 1:
            ap_mac = dot11_pckt.addr1
            sta_mac = dot11_pckt.addr2
        elif dot11_pckt.isFlagSet('FCfield', 'from-DS') \
         and not int(dot11_pckt.addr1[1], 16) & 1:
            ap_mac = dot11_pckt.addr2
            sta_mac = dot11_pckt.addr1
        else:
            return

        # May result in 'anonymous' AP
        self._add_ap(ap_mac, dot11_pckt)
        ap = self.air[ap_mac]

        self._add_station(ap, sta_mac)
        sta = ap[sta_mac]

        if EAPOL_WPAKey in dot11_pckt:
            wpakey_pckt = dot11_pckt[EAPOL_WPAKey]
        elif EAPOL_RSNKey in dot11_pckt:
            wpakey_pckt = dot11_pckt[EAPOL_RSNKey]
        else:
            return

        # Frame 1: pairwise set, install unset, ack set, mic unset
        # results in ANonce
        if wpakey_pckt.areFlagsSet('KeyInfo', ('pairwise', 'ack')) \
         and wpakey_pckt.areFlagsNotSet('KeyInfo', ('install', 'mic')):
            self._add_keypckt(sta, 0, pckt)

        # Frame 2: pairwise set, install unset, ack unset, mic set,
        # WPAKeyLength > 0. Results in MIC and keymic_frame
        elif wpakey_pckt.areFlagsSet('KeyInfo', ('pairwise', 'mic')) \
         and wpakey_pckt.areFlagsNotSet('KeyInfo', ('install', 'ack')) \
         and wpakey_pckt.WPAKeyLength > 0:
            self._add_keypckt(sta, 1, pckt)

        # Frame 3: pairwise set, install set, ack set, mic set
        # Results in ANonce
        elif wpakey_pckt.areFlagsSet('KeyInfo', \
                                     ('pairwise', 'install', 'ack', 'mic')):
            self._add_keypckt(sta, 2, pckt)

    def __iter__(self):
        return [ap for essid, ap in sorted([(ap.essid, ap) \
                               for ap in self.air.itervalues()])].__iter__()

    def __getitem__(self, bssid):
        return self.air[bssid]

    def __contains__(self, bssid):
        return bssid in self.air

    def __len__(self):
        return len(self.air)


class EAPOLCrackerThread(threading.Thread, _cpyrit_cpu.EAPOLCracker):

    def __init__(self, workqueue, auth):
        threading.Thread.__init__(self)
        _cpyrit_cpu.EAPOLCracker.__init__(self, auth.version, auth.pke,
                                            auth.keymic, auth.keymic_frame)
        self.workqueue = workqueue
        self.shallStop = False
        self.solution = None
        self.numSolved = 0
        self.setDaemon(True)
        self.start()

    def run(self):
        while not self.shallStop:
            try:
                results = self.workqueue.get(block=True, timeout=0.5)
            except Queue.Empty:
                pass
            else:
                solution = self.solve(results)
                self.numSolved += len(results)
                if solution:
                    self.solution = solution[0]
                self.workqueue.task_done()


class EAPOLCracker(object):

    def __init__(self, authentication):
        self.queue = Queue.Queue(10)
        self.workers = []
        self.solution = None
        for i in xrange(util.ncpus):
            self.workers.append(EAPOLCrackerThread(self.queue, authentication))

    def _getSolution(self):
        if self.solution is None:
            for worker in self.workers:
                if worker.solution is not None:
                    self.solution = worker.solution
                    break

    def enqueue(self, results):
        self.queue.put(results)
        self._getSolution()

    def join(self):
        self.queue.join()
        for worker in self.workers:
            worker.shallStop = True
        self._getSolution()

    def __len__(self):
        return sum(worker.numSolved for worker in self.workers)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.join()
