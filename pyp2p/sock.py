"""
Implements a custom protocol for sending and receiving
line delineated messages. For blocking sockets,
time-out is required to avoid DoS attacks when talking
to a misbehaving or malicious third party.

The benefit of this class is it makes communication
with the P2P network easy to code without having to
depend on threads and hence on mutexes (which are hard
to use correctly.)

In practice, a connection to a node on the P2P network
would be done using the default options of this class
and the connection would periodically be polled for
replies. The processing of replies would automatically
break once the socket indicated it would block and
to prevent a malicious node from sending replies as
fast as it could - there would be a max message limit
per check period.

Quirks:
* send_line will block until the entire line has been sent even if the socket has been set to non-blocking to make things easier. If you need a non-blocking way to send a line: use send(). Note that you will have to check for the number of bytes sent and resend if needed just like the real send function.
* connect has the same behaviour as above to make things simpler (so will block regardless of whether socket is in non-blocking mode or not.) If you want to bypass this behaviour you can always connect the socket outside this class and then pass it to set_socket.

Otherwise, all functions in this class behave how you would expect them to (depending on whether you're using non-blocking mode or blocking mode.) It's assumed that all blocking operations have a timeout by default. This can't be disabled.

"""

import socket
import time
import ssl
import select
import errno
import platform
from .lib import *

error_log_path = "error.log"

class Sock:
    def __init__(self, addr=None, port=None, blocking=0, timeout=5, interface="default", use_ssl=0, debug=0):
        self.reply_filter = None
        self.buf = u""
        self.max_buf = 1024 * 1024 # 1 MB.
        self.max_chunks = 1024 # Prevents spamming of multiple short messages.
        self.chunk_size = 100 * 1024
        self.replies = []
        self.blocking = blocking
        self.timeout = timeout
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.use_ssl = use_ssl
        if self.use_ssl:
            self.s = ssl.wrap_socket(self.s)
        self.connected = 0
        self.interface = interface
        self.delimiter = u"\r\n"
        self.debug = debug

        # Set a timeout for blocking operations so they don't DoS the program.
        # Disabled after connect if non-blocking is set.
        # (Connect is so far always blocking regardless of blocking mode.)
        self.s.settimeout(5)

        # When was the last connection alive check?
        self.last_heart_beat = time.time()

        # How often should we check for dead connections?
        self.heart_beat_interval = 5 * 60

        # Set keep alive.
        self.set_keep_alive(self.s)

        # Connect socket.
        if addr != None and port != None:
            self.connect(addr, port)

    def debug_print(self, msg):
        msg = "> " + str(msg)
        if self.debug:
            print(msg)

    def set_keep_alive(self, sock, after_idle_sec=1, interval_sec=3, max_fails=5):
        """
        This function instructs the TCP socket to send a heart beat every n seconds to detect dead connections. It's the TCP equivalent of the IRC ping-pong protocol and allows for better cleanup / detection of dead TCP connections.

It activates after 1 second (after_idle_sec) of idleness, then sends a keepalive ping once every 3 seconds (interval_sec), and closes the connection after 5 failed ping (max_fails), or 15 seconds
        """

        # OSX
        if platform.system() == "Darwin":
            # scraped from /usr/include, not exported by python's socket module
            TCP_KEEPALIVE = 0x10
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, TCP_KEEPALIVE, interval_sec)

        if platform.system() == "Windows":
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))

        if platform.system() == "Linux":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, after_idle_sec)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_sec)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, max_fails)

    def set_blocking(self, blocking, timeout=5):
        # Change blocking state.
        self.s.setblocking(blocking)

        # Adjust timeout if needed.
        if blocking:
            if timeout != None:
                self.s.settimeout(timeout)

        # Update blocking status.
        self.blocking = blocking

    def set_sock(self, s):
        self.close() # Close old socket.
        self.s = s
        self.connected = 1
        self.set_blocking(self.blocking, self.timeout)

        # Set keep alive.
        self.set_keep_alive(self.s)

        # Save addr + port.
        try:
            addr, port = self.s.getpeername()
            self.addr = addr
            self.port = port
        except:
            self.connected = 0

    def reconnect(self):
        if not self.connected:
            if self.addr != None and self.port != None:
                try:
                    return self.connect(self.addr, self.port)
                except:
                    self.connected = 0

    # Blocking (regardless of socket mode.)
    def connect(self, addr, port):
        # Save addr and port so socket can be reconnected.
        self.addr = addr
        self.port = port

        # No socket detected.
        if self.s == None:
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if self.use_ssl:
                self.s = ssl.wrap_socket(self.s)

        # Make connection from custom interface.
        if self.interface != "default":
            try:
                src_ip = get_lan_ip(self.interface)
                self.s.bind((src_ip, 0))
            except:
                # Already bound.
                pass

        try:
            self.s.connect((addr, int(port)))
            if not self.blocking:
                self.set_blocking(self.blocking, self.timeout)
            self.connected = 1
        except Exception as e:
            self.close()
            error = parse_exception(e)
            log_exception(error_log_path, error)
            raise socket.error("Socket connect failed.")

    def close(self):
        self.connected = 0

        # Attempt graceful shutdown.
        try:
            try:
                self.s.shutdown(socket.SHUT_RDWR)
            except:
                pass
            self.s.close()
        except:
            pass

        self.s = None

    def parse_buf(self):
        """
        Since TCP is a stream-orientated protocol, responses aren't guaranteed
        to be complete when they arrive. The buffer stores all the data and
        this function splits the data into replies based on the new line
        delimiter.
        """
        buf_len = len(self.buf)
        replies = []
        reply = u""
        chop = 0
        skip = 0
        i = 0
        for ch in self.buf:
            if skip:
                skip -= 1
                i += 1
                continue

            nxt = i + 1
            if nxt < buf_len:
                if ch == u"\r" and self.buf[nxt] == u"\n":
                    # Append new reply.
                    if reply != u"":
                        replies.append(reply)
                        reply = u""

                    # Truncate the whole buf if chop is out of bounds.
                    chop = nxt + 1
                    skip = 1
                    i += 1
                    continue

            reply += ch
            i += 1

        # Truncate buf.
        if chop:
            self.buf = self.buf[chop:]

        return replies

    # Blocking or non-blocking.
    def get_chunks(self, fixed_limit=None, encoding="unicode"):
        """
        This is the function which handles retrieving new data chunks. It's
        main logic is avoiding a recv call blocking forever and halting
        the program flow. To do this, it manages errors and keeps an eye
        on the buffer to avoid overflows and DoS attacks.

        http://stackoverflow.com/questions/16745409/what-does-pythons-socket-recv-return-for-non-blocking-sockets-if-no-data-is-r
        http://stackoverflow.com/questions/3187565/select-and-ssl-in-python
        """

        # Socket is disconnected.
        if not self.connected:
            return

        # Recv chunks until network buffer is empty.
        repeat = 1
        wait = 0.2
        chunk_no = 0
        max_buf = self.max_buf
        max_chunks = self.max_chunks
        if fixed_limit != None:
            max_buf = fixed_limit
            max_chunks = fixed_limit

        while repeat:
            chunk_size = self.chunk_size
            while True:
                # Don't exceed buffer size.
                buf_len = len(self.buf)
                if buf_len >= max_buf:
                    break
                remaining = max_buf - buf_len
                if remaining < chunk_size:
                    chunk_size = remaining

                # Don't allow non-blocking sockets to be
                # DoSed by multiple small replies.
                if chunk_no >= max_chunks and not self.blocking:
                    break
                
                try:
                    chunk = self.s.recv(chunk_size)
                except socket.timeout as e:
                    self.debug_print("Get chunks timed out.")
                    self.debug_print(e)

                    # Timeout on blocking sockets.
                    err = e.args[0]
                    self.debug_print(err)
                    if err == "timed out":
                        repeat = 0
                        break
                except ssl.SSLError as e:
                    # Will block on non-blocking SSL sockets.
                    if e.errno == ssl.SSL_ERROR_WANT_READ:
                        break
                    else:
                        self.close()
                        return
                except socket.error as e:
                    # Will block on nonblocking non-SSL sockets.
                    self.debug_print("Get chunks socket.error")
                    err = e.args[0]
                    self.debug_print(err)
                    if err == errno.EAGAIN or err == errno.EWOULDBLOCK:
                        # Check connection isn't dead:
                        if time.time() - self.last_heart_beat >= self.heart_beat_interval:
                            self.last_heart_beat = time.time()
                            self.send_line("PING")

                        break
                    else:
                        # Connection closed or other problem.
                        self.close()
                        return
                else:
                    if chunk == b"":
                        self.debug_print("Get chunk: b''")
                        self.close()
                        return

                    # Avoid decoding errors.
                    try:
                        if encoding == "unicode":
                            self.buf += chunk.decode("utf-8")
                        else:
                            self.buf += chunk.decode("latin-1")
                    except Exception as e:
                        self.debug_print(e)
                        self.debug_print("Get chunk: can't decode.")
                        chunk_no += 1
                        continue

                    if self.blocking:
                        break

                    chunk_no += 1

            # Repeat is already set -- manual skip.
            if not repeat:
                break
            else:
                repeat = 0

            # Block until there's a full reply or there's a timeout.
            if self.blocking:
                if fixed_limit == None and encoding == "unicode":
                    # Partial response.
                    if self.delimiter not in self.buf:
                        repeat = 1
                        time.sleep(wait)

    def reply_callback(self, callback):
        self.reply_callback = callback

    # Called to check for replies and update buffers.
    def update(self):
        self.get_chunks()
        self.replies = self.parse_buf()

    # Blocking or non-blocking.
    def send(self, msg, send_all=0, timeout=5):
        # Update timeout.
        if self.blocking and timeout != None:
            self.set_blocking(1, timeout)

        try:
            # Not connected.
            if not self.connected:
                return 0

            total_sent = 0

            # Convert to bytes Python 2 & 3
            if sys.version_info >= (3,0,0):
                if type(msg) == str:
                    msg = msg.encode("ascii")
            else:
                if type(msg) == unicode:
                    msg = str(msg)

            while True:
                # Attempt to send all.
                # This won't work if the network buffer is already full.
                bytes_sent = self.s.send(msg[total_sent:])

                # Connection broken.
                if not bytes_sent or bytes_sent == None:
                    self.close()
                    break

                # How much has been sent?
                total_sent += bytes_sent

                # Send the rest if blocking:
                if not (total_sent < len(msg) and (self.blocking or send_all)):
                    break

            return total_sent
        except Exception as e:
            error = parse_exception(e)
            log_exception(error_log_path, error)
            self.close()
            return 0
        finally:
            self.set_blocking(self.blocking, self.timeout)

    # Blocking or non-blocking.
    def recv(self, n, encoding="unicode", timeout=10):
        # Update timeout.
        if self.blocking and timeout != None:
            self.set_blocking(1, timeout)

        try:
            # Disconnect.
            if not self.connected:
                if encoding == "unicode":
                    return u""
                else:
                    return b""

            # Save current buffer state.
            temp_buf = self.buf[:]

            # Clear buffer.
            self.buf = u""

            # Get data.
            while True:
                self.get_chunks(n, encoding=encoding)
                if not (len(self.buf) < n and self.connected and self.blocking):
                    break

            # Save current buffer.
            ret = self.buf[:]

            # Restore old buffer.
            self.buf = temp_buf

            # Return results.
            if encoding != "unicode":
                # Convert from unicode string with latin-1 encoding
                # To a byte string.
                if sys.version_info >= (3,0,0):
                    codes = []
                    for ch in ret:
                        codes.append(ord(ch))

                    return bytes(codes)
                else:
                    byte_str = b""
                    for ch in ret:
                        byte_str += chr(ord(ch))

                    return byte_str

            return ret
        except Exception as e:
            error = parse_exception(e)
            log_exception(error_log_path, error)
            self.close()
            if encoding == "unicode":
                return u""
            else:
                return b""
        finally:
            self.set_blocking(self.blocking, self.timeout)

    # Sends a new message delimitered by a new line.
    # Blocking: blocks until entire line is sent for simplicity.
    def send_line(self, msg, timeout=5):
        # Update timeout.
        if self.blocking and timeout != None:
            self.set_blocking(1, timeout)

        try:
            # Not connected.
            if not self.connected:
                return 0

            # Convert to bytes Python 2 & 3
            if sys.version_info >= (3,0,0):
                if type(msg) == str:
                    msg = msg.encode("ascii")
            else:
                if type(msg) == unicode:
                    msg = str(msg)

            # Convert delimiter to bytes.
            if sys.version_info >= (3,0,0):
                msg += self.delimiter.encode("ascii")
            else:
                msg += str(self.delimiter)

            """
            The inclusion of the send_all flag makes this function behave like a blocking socket for the purposes of sending a full line even if the socket is non-blocking. It's assumed that lines will be small and if the network buffer is full this code won't end up as a bottleneck. (Otherwise you would have to check the number of bytes returned every time you sent a line which is quite annoying.)
            """
            ret = self.send(msg, send_all=1, timeout=timeout)

            return ret
        except Exception as e:
            error = parse_exception(e)
            log_exception(error_log_path, error)
            self.close()
            return 0
        finally:
            self.set_blocking(self.blocking, self.timeout)

    # Receives a new message delimited by a new line.
    # Blocking or non-blocking.
    def recv_line(self, timeout=2):
        # Update timeout.
        if self.blocking and timeout != None:
            self.set_blocking(1, timeout)

        old_buf = self.buf[:]
        self.buf = u""
        try:
            t = time.time() + timeout
            while True:
                self.update()

                # Socket is disconnected.
                if not self.connected:
                    return u""

                # Non-blocking.
                if not ((not len(self.replies) or len(self.buf) >= self.max_buf) and self.blocking):
                    break

                # Timeout elapsed.
                if time.time() >= t and self.blocking:
                    break

            if self.blocking:
                if len(self.replies):
                    temp = self.replies[0]
                    self.replies = self.replies[1:]
                    return temp

            return u""
        finally:
            self.set_blocking(self.blocking, self.timeout)
            self.buf = old_buf

    """
    These functions here make the class behave like a list. The
    list is a collection of replies received from the socket.
    Every iteration also has the bonus of checking for any
    new replies so it is very easy, for example to do:
    for replies in sock:
        To process replies without handling networking boilerplate.
    """
    def __len__(self):
        self.update()
        return len(self.replies)

    def __getitem__(self, key):
        self.update()
        return self.replies[key]

    def __setitem__(self, key, value):
        self.update()
        self.replies[key] = value

    def __delitem__(self, key):
        self.update()
        del self.replies[key]

    def pop_reply(self):
        # Get replies.
        replies = []
        for reply in self.replies:
            replies.append(reply)

        if len(replies):
            # Put replies back in the queue.
            self.replies = replies[1:]

            # Return the first reply.
            return replies[0]
        else:
            return None

    def __iter__(self):
        # Get replies.
        self.update()

        # Execute callbacks on replies.
        if self.reply_filter != None:
            replies = list(filter(self.reply_filter, self.replies))
        else:
            replies = self.replies

        # Clear old replies.
        self.replies = []

        # Return replies.
        return iter(replies)

    def __reversed__(self):
        return self.__iter__()

if __name__ == "__main__":
    s = Sock("158.69.201.105", 8540)

    exit()
    s.send_line("SOURCE TCP")


    while 1:
        for reply in s:
            print(reply)

        time.sleep(0.5)


    # print(s.recv_line())
    # print("yes")



    # def __init__(self, addr=None, port=None, blocking=0, timeout=5, interface="default", use_ssl=0):
