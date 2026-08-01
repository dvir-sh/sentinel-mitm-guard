# runs on Railway — no need to run this locally
import socket
import threading
import ssl


def relay(source, destination):
    """Blindly forward bytes in one direction until the connection drops."""
    try:
        while True:
            data = source.recv(4096)
            if not data:
                break
            destination.sendall(data)
    except Exception:
        pass
    finally:
        try:
            source.shutdown(socket.SHUT_RD)
        except Exception:
            pass
        try:
            destination.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def handle_client(client_socket):
    try:
        request = client_socket.recv(4096)
        if not request:
            client_socket.close()
            return

        if request.startswith(b'CONNECT'):
            # HTTPS tunnel: client sends CONNECT host:port, we open the TCP pipe
            first_line = request.split(b'\n')[0]
            try:
                _, target, _ = first_line.split(b' ')
                target_host, target_port = target.split(b':')
                target_host = target_host.decode('utf-8')
                target_port = int(target_port.decode('utf-8'))
            except Exception:
                client_socket.close()
                return

            try:
                server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server_socket.connect((target_host, target_port))
            except Exception:
                client_socket.send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                client_socket.close()
                return

            client_socket.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            t1 = threading.Thread(target=relay, args=(client_socket, server_socket))
            t2 = threading.Thread(target=relay, args=(server_socket, client_socket))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        else:
            # plain HTTP: parse the destination from the request line and forward it
            first_line = request.split(b'\n')[0]
            try:
                method, url, protocol = first_line.split(b' ')
            except Exception:
                client_socket.close()
                return

            http_pos = url.find(b'://')
            if http_pos != -1:
                url = url[http_pos + 3:]

            port = 80
            webserver_pos = url.find(b'/')
            if webserver_pos == -1:
                webserver_pos = len(url)
            port_pos = url.find(b':')

            if port_pos != -1 and port_pos < webserver_pos:
                try:
                    port = int(url[port_pos + 1:webserver_pos])
                except ValueError:
                    port = 80
                webserver = url[:port_pos]
            else:
                webserver = url[:webserver_pos]

            try:
                server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server_socket.connect((webserver.decode('utf-8'), port))
                server_socket.sendall(request)
            except Exception:
                client_socket.close()
                return

            while True:
                data = server_socket.recv(4096)
                if data:
                    client_socket.sendall(data)
                else:
                    break
            server_socket.close()

    except Exception as e:
        print("error handling client:", e)
    finally:
        client_socket.close()


def main():
    listen_addr = '0.0.0.0'
    listen_port = 8443

    proxy_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_server.bind((listen_addr, listen_port))
    proxy_server.listen(5)
    print(f"proxy listening on {listen_addr}:{listen_port}")

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(certfile="server.crt", keyfile="server.key")

    while True:
        client_sock, addr = proxy_server.accept()
        try:
            secure_sock = ssl_context.wrap_socket(client_sock, server_side=True)
        except Exception as e:
            print("SSL handshake failed:", e)
            client_sock.close()
            continue

        client_handler = threading.Thread(target=handle_client, args=(secure_sock,))
        client_handler.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        input("\nshutting down — press Enter to exit.")
