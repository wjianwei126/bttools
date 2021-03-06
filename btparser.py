#!/usr/bin/env python

import bencode

import argparse
import hashlib
import logging
import sys
import struct
from socket import ntohl, ntohs
from collections import defaultdict

class BitTorrentParserError(Exception):
    pass

class UnexpectedEndOfStreamError(BitTorrentParserError):
    def __init__(self, msg='Unexpected end of stream.'):
        super(UnexpectedEndOfStreamError, self).__init__(msg)

class InvalidBitTorrentStreamError(BitTorrentParserError):
    def __init__(self, msg='Invalid BitTorrent stream.'):
        super(InvalidBitTorrentStreamError, self).__init__(msg)

EXT_ALLOWED_FAST = 1

message_parsers = {}
extended_message_parsers = {}

# maps extended message numbers to their names
extended_message_associtions = {}

def register_message(n):
    def decorator(f):
        def wrapper(*args, **kwargs):
            f(*args, **kwargs)
        message_parsers[n] = f
        return wrapper
    return decorator

def register_extended_message(n):
    def decorator(f):
        def wrapper(*args, **kwargs):
            f(*args, **kwargs)
        extended_message_parsers[n] = f
        return wrapper
    return decorator

class BitTorrentParser(object):
    def __init__(self):
        self.infos = {}
        self.current_infohash = ''
        self.current_peerid = ''

        self.logger = logging.getLogger('utptrace')
        self.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        self.logger.addHandler(handler)

    def add_info(self, info):
        self.infos[hashlib.sha1(bencode.bencode(info)).digest()] = info

    def parse_stream(self, stream):
        if len(stream) < 68:
            raise UnexpectedEndOfStreamError(
                'The stream is less than 68 bytes long.')

        pstrlen, pstr, reserved, infohash, peerid = struct.unpack('!B19s8s20s20s', stream[:68])
        self.current_infohash = infohash
        self.current_peerid = peerid

        if pstrlen != 19 or pstr != 'BitTorrent protocol':
            msg = 'Stream does not contain BitTorrent data.'
            self.logger.error(msg)
            raise InvalidBitTorrentStreamError(msg)

        self.logger.info('pstr: {}'.format(pstr))
        self.logger.info('reserved: {}'.format(reserved.encode('hex')))
        self.logger.info('infohash: {}'.format(infohash.encode('hex')))
        self.logger.info('peerid: {}'.format(peerid.encode('hex')))

        n = 68
        while n < len(stream):
            n = self.parse_message(stream, n)

    def parse_message(self, stream, n):
        if n + 4 >= len(stream):
            raise UnexpectedEndOfStreamError()

        length = ntohl(struct.unpack('I', stream[n:n + 4])[0])
        if length == 0:
            return n + 4

        id = ord(stream[n + 4])

        try:
            if length > 16393:
                self.logger.warning('Message length is over 16393. Possibly corrupt.')
            if n + 4 + length > len(stream):
                raise UnexpectedEndOfStreamError()

            message_parsers[id](self, stream, n, length)
        except KeyError as e:
            self.logger.warning('[MESSAGE] UNKNOWN MESSAGE ID: {}'.format(id))
            self.__new_message('unknown', message_id=id)

        return n + length + 4

    @register_extended_message('ut_metadata')
    def parse_message_ut_metadata(self, stream, n, length):
        try:
            ut_metadata = bencode.bdecode(stream[n+6:n+length+4])
        except bencode.BTL.BTFailure:
            raise InvalidBitTorrentStreamError()
        if ut_metadata['msg_type'] == 0:
            self.logger.info(
                '[MESSAGE] [EXTENDED] ut_metadata: request for piece {}'.format(
                    ut_metadata['piece']))
        elif ut_metadata['msg_type'] == 1:
            size = length - 2 - bencode.bencode(ut_metadata)
            self.logger.info(
                '[MESSAGE] [EXTENDED] ut_metadata: piece {} of size {}'.format(
                    ut_metadata['piece'], size))
        elif ut_metadata['msg_type'] == 2:
            self.logger.info(
                '[MESSAGE] [EXTENDED] ut_metadata: reject request for piece {}'.format(
                    ut_metadata['piece']))
        self.__new_extended_message('ut_metadata', piece=ut_metadata['piece'])

    @register_extended_message('upload_only')
    def parse_message_upload_only(self, stream, n, length):
        payload = stream[n+6:n+length+4]
        self.logger.info('[MESSAGE] [EXTENDED] upload_only: turned {}'.format(
            'off' if payload[0] == '\x00' else 'on'))
        self.__new_extended_message('upload_only', value=(payload[0] != '\x00'))

    @register_extended_message('lt_tex')
    def parse_message_lt_tex(self, stream, n, length):
        try:
            lt_tex = bencode.bdecode(stream[n+6:n+length+4])
        except bencode.BTL.BTFailure:
            raise InvalidBitTorrentStreamError()
        self.logger.info('[MESSAGE] [EXTENDED] lt_tex: announced {} tracker(s).'.format(
            len(lt_tex['added'])))
        self.__new_extended_message('lt_tex', added=lt_tex['added'])

    @register_extended_message('ut_pex')
    def parse_message_ut_pex(self, stream, n, length):
        try:
            ut_pex = bencode.bdecode(stream[n+6:n+length+4])
        except bencode.BTL.BTFailure:
            raise InvalidBitTorrentStreamError()
        added = ut_pex['added']
        prefer_encryption = len([i for i in ut_pex['added.f'] if ord(i) & 0x01 == 1])
        seeders = len([i for i in ut_pex['added.f'] if ord(i) & 0x02 == 1])
        self.logger.info(
            '[MESSAGE] [EXTENDED] ut_pex: added {} peers ({} prefer(s) '
            'encryption; {} is/are seeder(s)). dropped {}.'.format(
                len(added) / 6, # In compact form, each 6 bytes
                                # represents an IPv4 address and a
                                # port number.
                prefer_encryption,
                seeders,
                len(ut_pex['dropped']) / 6))

        if all(k in ut_pex for k in ['added6', 'added6.f', 'dropped6']) and \
           (len(ut_pex['added6']) > 0 or len(ut_pex['dropped6']) > 0):
            added = ut_pex['added6']
            prefer_encryption = len([i for i in ut_pex['added6.f'] if ord(i) & 0x01 == 1])
            seeders = len([i for i in ut_pex['added6.f'] if ord(i) & 0x02 == 1])
            self.logger.info(
                '[MESSAGE] [EXTENDED]         also added {} IPv6 peers '
                '({} prefer(s) encryption; {} is/are seeder(s)). '
                'dropped {}.'.format(
                    len(added) / 18, # In compact form, each 18 bytes
                                     # represents an IPv6 address and
                                     # a port number.
                    prefer_encryption,
                    seeders,
                    len(ut_pex['dropped6']) / 18))

        self.__new_extended_message('ut_pex', value=ut_pex)

    @register_message(20)
    def parser_message_extended(self, stream, n, length):
        n += 5
        id = ord(stream[n])
        n += 1
        if id == 0:
            handshake = stream[n:n + (length - 2)]
            handshake = bencode.bdecode(handshake)
            self.logger.info('[MESSAGE] [EXTENDED] HANDSHAKE: {}'.format(handshake))

            for name, number in handshake['m'].items():
                if number == 0:
                    # disable this extension
                    extended_message_associations = {
                        k: v for k, v in extended_message_associtions.items()
                        if v != name}
                else:
                    # add this extension
                    extended_message_associtions[number] = name
        elif id in extended_message_associtions:
            name = extended_message_associtions[id]
            if name not in extended_message_parsers:
                self.logger.info('[MESSAGE][EXTENDED] "{}" message.'.format(name))
                self.new_extended_message(name)
                return
            extended_message_parsers[name](self, stream, n - 6, length)
        else:
            self.logger.warning(
                '[MESSAGE] [EXTENDED] UNKNOWN MESSAGE ID: {}'.format(id))
            # this is not a valid message (the id used has not been
            # defined in the handshake), so we won't call
            # self.__new_extended_message.

    @register_message(0)
    def parse_message_choke(self, stream, n, length):
        self.logger.info('[MESSAGE] CHOKE')
        self.__new_message('choke')

    @register_message(1)
    def parse_message_unchoke(self, stream, n, length):
        self.logger.info('[MESSAGE] UNCHOKE')
        self.__new_message('unchoke')

    @register_message(2)
    def parse_message_interested(self, stream, n, length):
        self.logger.info('[MESSAGE] INTERESTED')
        self.__new_message('interested')

    @register_message(3)
    def parse_message_not_interested(self, stream, n, length):
        self.logger.info('[MESSAGE] NOT INTERESTED')
        self.__new_message('not_interested')

    @register_message(4)
    def parse_message_have(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        self.logger.info('[MESSAGE] HAVE: {}'.format(index))
        self.__new_message('have', index=index)

    @register_message(5)
    def parse_message_bitfield(self, stream, n, length):
        bitfield = stream[n+5:n+length]
        bitfield_str = ''.join(bin(ord(i))[2:] for i in stream[n+5:n+length])
        self.logger.info('[MESSAGE] BITFIELD: {}'.format(bitfield_str))
        self.__new_message('bitfield', bitfield=bitfield)

    @register_message(6)
    def parse_message_request(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        begin = ntohl(struct.unpack('I', stream[n+9:n+13])[0])
        length = ntohl(struct.unpack('I', stream[n+13:n+17])[0])
        self.logger.info(
            '[MESSAGE] REQUEST: index={} begin={} length={}'.format(
                index, begin, length))
        self.__new_message('request', index=index, begin=begin, length=length)

    @register_message(7)
    def parse_message_piece(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        begin = ntohl(struct.unpack('I', stream[n+9:n+13])[0])
        block_size = length - 1 - 8
        data = stream[n+13:n+13+length-1-8]
        assert(len(data) == block_size)
        self.logger.info(
            '[MESSAGE] PIECE: index={} begin={} length={}'.format(
                index, begin, block_size))
        self.__new_message('piece', index=index, begin=begin, data=data)

    @register_message(8)
    def parse_message_choke(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        begin = ntohl(struct.unpack('I', stream[n+9:n+13])[0])
        length = ntohl(struct.unpack('I', stream[n+13:n+17])[0])
        self.logger.info(
            '[MESSAGE] CANCEL: index={} begin={} length={}'.format(
                index, begin, length))
        self.__new_message('cancel', index=index, begin=begin, length=length)

    @register_message(9)
    def parse_message_port(self, stream, n, length):
        port = ntohs(struct.unpack('H', stream[n+5:n+7])[0])
        self.logger.info('[MESSAGE] PORT: {}'.format(port))
        self.__new_message('port', port=port)

    @register_message(0x0d)
    def parse_message_suggest_piece(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        self.logger.info('[MESSAGE] SUGGEST PIECE: {}'.format(index))
        self.__new_message('suggest_piece', index=index)

    @register_message(0x0e)
    def parse_message_have_all(self, stream, n, length):
        self.logger.info('[MESSAGE] HAVE ALL')
        self.__new_message('have_all')

    @register_message(0x0f)
    def parse_message_have_none(self, stream, n, length):
        self.logger.info('[MESSAGE] HAVE NONE')
        self.__new_message('have_none')

    @register_message(0x10)
    def parse_message_reject(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        begin = ntohl(struct.unpack('I', stream[n+9:n+13])[0])
        length = ntohl(struct.unpack('I', stream[n+13:n+17])[0])
        self.logger.info(
            '[MESSAGE] REJECT: index={} begin={} length={}'.format(
                index, begin, length))
        self.__new_message('reject')

    @register_message(0x11)
    def parse_message_allowed_fast(self, stream, n, length):
        index = ntohl(struct.unpack('I', stream[n+5:n+9])[0])
        self.logger.info('[MESSAGE] ALLOWED FAST: {}'.format(index))
        self.__new_message('allowed_fast')

    def __new_extended_message(self, name, **attrs):
        self.__new_message(20, extension_name=name, **attrs)
        self.new_extended_message(name, **attrs)

    def __new_message(self, name, **attrs):
        self.new_message(name, **attrs)

    def new_message(self, name, **attrs):
        pass

    def new_extended_message(self, name, **attrs):
        pass

class MyBitTorrentParser(BitTorrentParser):
    def __init__(self):
        super(MyBitTorrentParser, self).__init__()
        self.pieces = defaultdict(list)
        self.current_pieces_size = 0

    def new_message(self, name, **attrs):
        if name != 'piece':
            return

        index = attrs['index']
        begin = attrs['begin']
        data = attrs['data']

        self.current_pieces_size += len(data)
        self.pieces[(self.current_infohash, index)].append((data, begin))
        self.check_piece(index)

    def check_piece(self, index):
        if self.current_infohash not in self.infos:
            return

        info = self.infos[self.current_infohash]
        piece_length = info['piece length']

        blocks = self.pieces[(self.current_infohash, index)]
        blocks.sort(key=lambda r: r[1])

        piece = ''
        n = 0
        while n < piece_length:
            b = [i[0] for i in blocks if i[1] == n]
            if b == []:
                break

            b = b[0]
            piece += b

            blocks = [i for i in blocks if i[1] != n]

            n += len(b)

        if len(piece) == piece_length:
            self.logger.info('Piece complete: {}'.format(index))
            got_hash = hashlib.sha1(piece).digest()
            expected_hash = info['pieces'][index*20:index*20+20]

            if got_hash == expected_hash:
                self.logger.info('Hash match for piece: {}'.format(index))
            else:
                self.logger.warning('Hash did not match for piece: {}'.format(index))

            del self.pieces[(self.current_infohash, index)]

def parse_file(filename, parser):
    parser.logger.info('[NEW FILE] {}'.format(filename))
    with open(filename) as f:
        stream = f.read()

    try:
        parser.current_pieces_size = 0
        parser.parse_stream(stream)
    except BitTorrentParserError as e:
        parser.logger.error(str(e))

    parser.logger.info(
        '{} bytes of piece data in a stream of {} bytes.'.format(
            parser.current_pieces_size,
            len(stream)))

def parse_directory(directory, parser):
    import os
    for filename in os.listdir(directory):
        parse_file(directory + '/' + filename, parser)

def main():
    parser = argparse.ArgumentParser(
        description='A program for parsing and analyzing BitTorrent streams.',
        epilog='Either -d or -f (but not both) must be used.')
    parser.add_argument(
        '-t', '--torrent', action='append',
        help='A torrent file to read and use in processing. '
        'The pieces with an infohash matching this file are checked'
        'against the hashes in this file. Can be specified multiple times.')
    parser.add_argument(
        '-d', '--directory',
        help='A directory to read the streams from.')
    parser.add_argument(
        '-f', '--filename',
        help='A file containing a BitTorrent stream.')
    parser.add_argument(
        '-l', '--log-file', dest='log_file',
        help='The file to log to. Defaults to stdout.')
    args = parser.parse_args()

    if args.filename and args.directory:
        print 'Only one of -f and -d can be used.'
        exit(1)

    if not args.filename and not args.directory:
        print 'Either -d or -f must be specified.'
        exit(1)

    btparser = MyBitTorrentParser()
    logger = logging.getLogger('btparser')
    if args.log_file:
        handler = logging.FileHandler(args.log_file, 'w')
    else:
        handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(levelname)s]\t%(msg)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    btparser.logger = logger

    if args.torrent:
        for tf in args.torrent:
            with open(tf) as f:
                btparser.add_info(bencode.bdecode(f.read())['info'])
    else:
        logger.warning('No torrent files specified.')

    if args.filename:
        parse_file(args.filename, btparser)

    if args.directory:
        parse_directory(args.directory, btparser)

    return

if __name__ == '__main__':
    main()
