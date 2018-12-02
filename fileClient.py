import queue
import socket
import json
from fileUtils import AvailableFile, File, Chunk
import threading
from threading import Lock
import asyncio
from config import *
from utils import send_packet
from _thread import start_new_thread


class FileClient():
    def __init__(self, send_file_callback):
        self.available_files = {}
        self.active_peers = 0
        self.send_file_callback = send_file_callback
        self.lock = Lock()

    def start(self):
        self.listen_discovery()

    def handle_file_definition(self, message):
        source, type, dict = message.split('|')
        file_list = json.loads(dict)
        for file in file_list:
            if file['checksum'] in self.available_files:
                self.available_files[file['checksum']].add_peer(source)
            else:
                self.available_files[file['checksum']] = AvailableFile(file['name'], file['checksum'],
                                                                       file['chunk_size'], source)
        if type == MESSAGE_TYPES["request"]:
            self.send_file_callback(source, MESSAGE_TYPES["response"])

    def receive_discovery(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((SELF_IP, DISCOVERY_PORT))
            s.listen()

            while True:
                conn, addr = s.accept()
                with conn:
                    message = ""
                    while True:
                        data = conn.recv(1024)
                        if not data:
                            self.handle_file_definition(message)
                            conn.close()
                            break
                        message = message + data.decode('utf_8')

    def send_chunk_requests(self):
        while True:
            for file in self.available_files.values():
                if file.status == "downloading":
                    for peer in file.peers:
                        requested_chunks = file.get_batch_new_chunks()
                        self.send_chunk_request(peer, file.checksum, requested_chunks)

    def send_chunk_request(self, target_ip, checksum, chunks):
        message = SELF_IP + "|" + checksum + "|" + json.dumps([chunk.offset for chunk in chunks])
        for chunk in chunks:
            chunk.status = "in_flight"
        start_new_thread(send_packet, (target_ip, CHUNK_PORT, message,))

    def start_client(self):
        chunk_request_thread = threading.Thread(target=self.send_chunk_requests())
        chunk_request_thread.setDaemon(True)
        chunk_request_thread.start()

    def listen_discovery(self):
        discovery_thread = threading.Thread(target=self.receive_discovery)
        discovery_thread.setDaemon(True)
        discovery_thread.start()

    def start_download(self, checksum):
        file = self.available_files[checksum]
        file.status = "downloading"
        file.start_download()
        self.lock.acquire()
        self.active_peers += len(file.peers)
        self.lock.release()

    def end_download(self, checksum):
        file = self.available_files[checksum]
        file.status = "finished"
        self.lock.acquire()
        self.active_peers -= len(file.peers)
        self.lock.release()
        file.save_to_shared()

class FileClientConnection:
    def __init__(self, client):
        self.buffer = queue.Queue(maxsize=DEFAULT_WINDOW_SIZE)
        self.window_size = DEFAULT_WINDOW_SIZE
        self.transport = None
        self.client = client

    def connection_made(self, transport):
        self.transport = transport

    def start(self):
        for peer in self.file.peers:
            chunks = self.file.get_batch_new_chunks(self.file.chunk_size // len(self.file.peers))
            self.send_chunk_request(peer, chunks)

    async def queue_handler(self):
        while True:
            try:
                item = self.buffer.get(block=False)
                chunk = self.file.chunks[item[0]]
                chunk.data = item[1]
                chunk.lock.acquire()
                chunk.status = "done"
                chunk.lock.release()
                if len([1 for chnk in self.file.chunks if chnk.status != "done"]) == 0:
                    self.client.end_download(self.file.checksum)
            except:
                pass
                # queue is empty here
            await asyncio.sleep(DRAINAGE)

    async def check_packets(self):
        pass

    # TODO: add loop for packet checking

    def datagram_received(self, data, addr):
        message = data.decode()
        checksum, offset, *payload = message.split("|")
        payload = "".join(payload)
        self.client.lock.acquire()
        rwindow = str((self.window_size - self.buffer.qsize()) // len(self.client.active_peers))
        return_msg = checksum + "|" + offset + "|" + rwindow
        self.client.lock.release()
        self.buffer.put((offset, payload))
        self.transport.sendto(return_msg.encode(), addr)

    def send_chunk_request(self, target_ip, chunks):
        message = SELF_IP + "|" + self.file.checksum + "|" + json.dumps([chunk.offset for chunk in chunks])
        for chunk in chunks:
            chunk.status = "pending"
        start_new_thread(send_packet, (target_ip, CHUNK_PORT, message,))

async def start_listener(client):
    loop = asyncio.get_running_loop()
    listener = FileClientConnection(client)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: listener,
        local_addr=('192.168.0.39', ACK_PORT))
    asyncio.ensure_future(listener.queue_handler())
    try:
        await asyncio.sleep(3600)  # Serve for 1 hour.
    finally:
        transport.close()

def start_download_queue(client):
    start_new_thread(asyncio.run,(start_listener(client),))