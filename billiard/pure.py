#
# Pure Python Connection implementation
# (requires Py2.7/Py3.3)
#
# billiard/pure.py
#
# Copyright (c) 2006-2008, R Oudkerk
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of author nor the names of any contributors may be
#    used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
from __future__ import absolute_import

__all__ = ['Connection', 'wait']

import io
import os
import pickle
import select
import struct
import errno
import time

from . import BufferTooShort


class _ConnectionBase:
    _handle = None

    def __init__(self, handle, readable=True, writable=True):
        handle = handle.__index__()
        if handle < 0:
            raise ValueError("invalid handle")
        if not readable and not writable:
            raise ValueError(
                "at least one of `readable` and `writable` must be True")
        self._handle = handle
        self._readable = readable
        self._writable = writable

    # XXX should we use util.Finalize instead of a __del__?

    def __del__(self):
        if self._handle is not None:
            self._close()

    def _check_closed(self):
        if self._handle is None:
            raise IOError("handle is closed")

    def _check_readable(self):
        if not self._readable:
            raise IOError("connection is write-only")

    def _check_writable(self):
        if not self._writable:
            raise IOError("connection is read-only")

    def _bad_message_length(self):
        if self._writable:
            self._readable = False
        else:
            self.close()
        raise IOError("bad message length")

    @property
    def closed(self):
        """True if the connection is closed"""
        return self._handle is None

    @property
    def readable(self):
        """True if the connection is readable"""
        return self._readable

    @property
    def writable(self):
        """True if the connection is writable"""
        return self._writable

    def fileno(self):
        """File descriptor or handle of the connection"""
        self._check_closed()
        return self._handle

    def close(self):
        """Close the connection"""
        if self._handle is not None:
            try:
                self._close()
            finally:
                self._handle = None

    def send_bytes(self, buf, offset=0, size=None):
        """Send the bytes data from a bytes-like object"""
        self._check_closed()
        self._check_writable()
        m = memoryview(buf)
        # HACK for byte-indexing of non-bytewise buffers (e.g. array.array)
        if m.itemsize > 1:
            m = memoryview(bytes(m))
        n = len(m)
        if offset < 0:
            raise ValueError("offset is negative")
        if n < offset:
            raise ValueError("buffer length < offset")
        if size is None:
            size = n - offset
        elif size < 0:
            raise ValueError("size is negative")
        elif offset + size > n:
            raise ValueError("buffer length < offset + size")
        self._send_bytes(m[offset:offset + size])

    def send(self, obj):
        """Send a (picklable) object"""
        self._check_closed()
        self._check_writable()
        buf = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        self._send_bytes(memoryview(buf))

    def recv_bytes(self, maxlength=None):
        """
        Receive bytes data as a bytes object.
        """
        self._check_closed()
        self._check_readable()
        if maxlength is not None and maxlength < 0:
            raise ValueError("negative maxlength")
        buf = self._recv_bytes(maxlength)
        if buf is None:
            self._bad_message_length()
        return buf.getvalue()

    def recv_bytes_into(self, buf, offset=0):
        """
        Receive bytes data into a writeable buffer-like object.
        Return the number of bytes read.
        """
        self._check_closed()
        self._check_readable()
        with memoryview(buf) as m:
            # Get bytesize of arbitrary buffer
            itemsize = m.itemsize
            bytesize = itemsize * len(m)
            if offset < 0:
                raise ValueError("negative offset")
            elif offset > bytesize:
                raise ValueError("offset too large")
            result = self._recv_bytes()
            size = result.tell()
            if bytesize < offset + size:
                raise BufferTooShort(result.getvalue())
            # Message can fit in dest
            result.seek(0)
            result.readinto(m[offset // itemsize:
                                (offset + size) // itemsize])
            return size

    def recv(self):
        """Receive a (picklable) object"""
        self._check_closed()
        self._check_readable()
        buf = self._recv_bytes()
        return pickle.loads(buf.getvalue())

    def poll(self, timeout=0.0):
        """Whether there is any input available to be read"""
        self._check_closed()
        self._check_readable()
        return self._poll(timeout)


class Connection(_ConnectionBase):
    """
    Connection class based on an arbitrary file descriptor (Unix only), or
    a socket handle (Windows).
    """

    def _close(self, _close=os.close):
        _close(self._handle)
    _write = os.write
    _read = os.read

    def _send(self, buf, write=_write):
        remaining = len(buf)
        while True:
            n = write(self._handle, buf)
            remaining -= n
            if remaining == 0:
                break
            buf = buf[n:]

    def _recv(self, size, read=_read):
        buf = io.BytesIO()
        handle = self._handle
        remaining = size
        while remaining > 0:
            chunk = read(handle, remaining)
            n = len(chunk)
            if n == 0:
                if remaining == size:
                    raise EOFError
                else:
                    raise IOError("got end of file during message")
            buf.write(chunk)
            remaining -= n
        return buf

    def _send_bytes(self, buf):
        # For wire compatibility with 3.2 and lower
        n = len(buf)
        self._send(struct.pack("!i", n))
        # The condition is necessary to avoid "broken pipe" errors
        # when sending a 0-length buffer if the other end closed the pipe.
        if n > 0:
            self._send(buf)

    def _recv_bytes(self, maxsize=None):
        buf = self._recv(4)
        size, = struct.unpack("!i", buf.getvalue())
        if maxsize is not None and size > maxsize:
            return None
        return self._recv(size)

    def _poll(self, timeout):
        if timeout < 0.0:
            timeout = None
        r = wait([self._handle], timeout)
        return bool(r)


def wait(object_list, timeout=None):
    '''
    Wait till an object in object_list is ready/readable.

    Returns list of those objects in object_list which are ready/readable.
    '''
    if timeout is not None:
        if timeout <= 0:
            return select.select(object_list, [], [], 0)[0]
        else:
            deadline = time.time() + timeout
    while True:
        try:
            return select.select(object_list, [], [], timeout)[0]
        except OSError as e:
            if e.errno != errno.EINTR:
                raise
        if timeout is not None:
            timeout = deadline - time.time()
