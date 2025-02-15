import select
import socket
import sys
import threading

class TextServer:
    '''Accepts TCP client connections and serves a continuous stream of text to all currently connected clients.'''
    def __init__(self, port):
        '''port: TCP port to listen on.'''
        # List of currently active connections to clients.
        self.connections = []

        # Lock for the connections list so the main thread doesn't write
        # while the listener thread is updating it.
        self.lock = threading.Lock()

        # Start the connection listener thread.
        def run_thread():
            self._listen(port)
        self.thread = threading.Thread(target=run_thread)
        self.thread.start()

    def write(self, text):
        '''Called by the main thread to write more text to the clients.'''
        with self.lock:
            idx = 0
            while idx < len(self.connections):
                conn = self.connections[idx]
                try:
                    conn.sendall(text.encode())
                except BrokenPipeError:
                    del self.connections[idx]
                idx += 1

    def _listen(self, port):
        '''Thread that listens for new clients.'''
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', port))
        sock.listen()
        print('Listening on port', port)
        sys.stdout.flush()
        while True:
            connection, _ = sock.accept()
            print('New connection')
            sys.stdout.flush()
            with self.lock:
                self.connections.append(connection)
