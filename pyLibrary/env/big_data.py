# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

# MIMICS THE requests API (http://docs.python-requests.org/en/latest/)
# WITH ADDED default_headers THAT CAN BE SET USING pyLibrary.debugs.settings
# EG
# {"debug.constants":{
# "pyLibrary.env.http.default_headers={
# "From":"klahnakoski@mozilla.com"
# }
# }}


from __future__ import unicode_literals
from __future__ import division
import gzip
from io import BytesIO
from tempfile import TemporaryFile
import zlib

from pyLibrary.debugs.logs import Log
from pyLibrary.maths import Math


MIN_READ_SIZE = 8 * 1024
MAX_STRING_SIZE = 1 * 1024 * 1024


class FileString(object):
    """
    ACTS LIKE A STRING, BUT IS A FILE
    """

    def __init__(self, file):
        self.file = file

    def decode(self, encoding):
        if encoding != "utf8":
            Log.error("can not handle {{encoding}}", {"encoding": encoding})
        self.encoding = encoding
        return self

    def split(self, sep):
        if sep != "\n":
            Log.error("Can only split by lines")
        self.file.seek(0)
        return LazyLines(self.file)

    def __len__(self):
        temp = self.file.tell()
        self.file.seek(0, 2)
        file_length = self.file.tell()
        self.file.seek(temp)
        return file_length

    def __getslice__(self, i, j):
        self.file.seek(i)
        output = self.file.read(j-i).decode(self.encoding)
        return output

    def __add__(self, other):
        self.file.seek(0, 2)
        self.file.write(other)

    def __radd__(self, other):
        new_file = TemporaryFile()
        new_file.write(other)
        self.file.seek(0)
        for l in self.file:
            new_file.write(l)
        new_file.seek(0)
        return FileString(new_file)

    def __getattr__(self, attr):
        return getattr(self.file, attr)

    def __del__(self):
        self.file, temp = None, self.file
        if temp:
            temp.close()

    def __iter__(self):
        self.file.seek(0)
        return self.file


def safe_size(source):
    """
    READ THE source UP TO SOME LIMIT, THEN COPY TO A FILE IF TOO BIG
    RETURN A str() OR A FileString()
    """

    total_bytes = 0
    bytes = []
    b = source.read(MIN_READ_SIZE)
    while b:
        total_bytes += len(b)
        bytes.append(b)
        if total_bytes > MAX_STRING_SIZE:
            data = FileString(TemporaryFile())
            for bb in bytes:
                data.write(bb)
            del bytes
            del bb
            b = source.read(MIN_READ_SIZE)
            while b:
                total_bytes += len(b)
                data.write(b)
                b = source.read(MIN_READ_SIZE)
            data.seek(0)
            Log.note("Using file of size {{length}} instead of str()", {"length": total_bytes})

            return data
        b = source.read(MIN_READ_SIZE)

    data = b"".join(bytes)
    del bytes
    return data


class LazyLines(object):
    """
    SIMPLE LINE ITERATOR, BUT WITH A BIT OF CACHING TO LOOK LIKE AN ARRAY
    """

    def __init__(self, source):
        """
        ASSUME source IS A LINE ITERATOR OVER utf8 ENCODED BYTE STREAM
        """
        self.source = source
        self._iter = self.__iter__()
        self._last = None
        self._next = 0

    def __getslice__(self, i, j):
        if i == self._next:
            return self._iter
        Log.error("Do not know how to slice this generator")

    def __iter__(self):
        def output():
            for v in self.source:
                self._last = v.decode("utf8")
                self._next += 1
                yield self._last

        return output()

    def __getitem__(self, item):
        try:
            if item == self._next:
                return self._iter.next()
            elif item == self._next - 1:
                return self._last
            else:
                Log.error("can not index out-of-order too much")
        except Exception, e:
            Log.error("Problem indexing", e)




class CompressedLines(LazyLines):
    """
    KEEP COMPRESSED HTTP (content-type: gzip) IN BYTES ARRAY
    WHILE PULLING OUT ONE LINE AT A TIME FOR PROCESSING
    """

    def __init__(self, compressed):
        """
        USED compressed BYTES TO DELIVER LINES OF TEXT
        LIKE LazyLines, BUT HAS POTENTIAL TO seek()
        """
        self.compressed = compressed
        LazyLines.__init__(self, None)
        self._iter = self.__iter__()

    def __iter__(self):
        return LazyLines(ibytes2ilines(bytes2ibytes(self.compressed, MIN_READ_SIZE))).__iter__()

    def __getslice__(self, i, j):
        if i == self._next:
            return self._iter
        Log.error("Do not know how to slice this generator")

    def __getitem__(self, item):
        try:
            if item == self._next:
                self._last = self._iter.next()
                self._next += 1
                return self._last
            elif item == self._next - 1:
                return self._last
            else:
                Log.error("can not index out-of-order too much")
        except Exception, e:
            Log.error("Problem indexing", e)


    def __radd__(self, other):
        new_file = TemporaryFile()
        new_file.write(other)
        self.file.seek(0)
        for l in self.file:
            new_file.write(l)
        new_file.seek(0)
        return FileString(new_file)


def bytes2ibytes(compressed, size):
    """
    CONVERT AN ARRAY TO A BYTE-BLOCK GENERATOR
    USEFUL IN THE CASE WHEN WE WANT TO LIMIT HOW MUCH WE FEED ANOTHER
    GENERATOR (LIKE A DECOMPRESSOR)
    """

    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)

    for i in range(0, Math.ceiling(len(compressed), size), size):
        try:
            block = compressed[i: i + size]
            yield decompressor.decompress(block)
        except Exception, e:
            Log.error("Not expected", e)


def ibytes2ilines(stream):
    """
    CONVERT A GENERATOR OF (ARBITRARY-SIZED) byte BLOCKS
    TO A LINE (CR-DELIMITED) GENERATOR
    """
    buffer = stream.next()
    s = 0
    e = buffer.find(b"\n")
    while True:
        while e == -1:
            try:
                buffer = buffer[s:] + stream.next()
                s = 0
                e = buffer.find(b"\n")
            except StopIteration:
                yield buffer[s:]
                del stream
                return

        yield buffer[s:e]
        s = e + 1
        e = buffer.find(b"\n", s)


class GzipLines(CompressedLines):
    """
    SAME AS CompressedLines, BUT USING THE GzipFile FORMAT FOR COMPRESSED BYTES
    """

    def __init__(self, compressed):
        CompressedLines.__init__(self, compressed)

    def __iter__(self):
        buff = BytesIO(self.compressed)
        return LazyLines(gzip.GzipFile(fileobj=buff, mode='r')).__iter__()
