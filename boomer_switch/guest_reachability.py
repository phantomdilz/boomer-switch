"""Offer a finite harmless marker without files, identity, permissions, or devices."""

import argparse
import ipaddress
import socket
import time


MARKER = b"BOOMER SWITCH REACHABILITY ONLY\nNo authentication or device control.\n"
RESPONSE = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\n"
            b"Connection: close\r\nContent-Length: " + str(len(MARKER)).encode() + b"\r\n\r\n" + MARKER)


def validate_address(value):
    address = ipaddress.ip_address(value)
    if address.version != 4 or address.is_global or address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:
        raise ValueError("Supply the selected private guest adapter address.")
    return str(address)


def serve_marker(address, port, seconds, *, clock=time.monotonic, socket_factory=socket.socket):
    address = validate_address(address)
    if not 1 <= port <= 65535 or not 1 <= seconds <= 120:
        raise ValueError("Use a valid port and a lifetime from 1 through 120 seconds.")
    deadline = clock() + seconds
    with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((address, port))
        listener.listen(2)
        print(f"Open http://{address}:{port}/ on the phone within {seconds} seconds.", flush=True)
        print("This page only checks reachability. Press Ctrl+C to stop.", flush=True)
        while clock() < deadline:
            listener.settimeout(min(1, deadline - clock()))
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                connection.settimeout(min(1, remaining))
                try:
                    request = connection.recv(2048)
                    if request:
                        remaining = deadline - clock()
                        if remaining > 0:
                            connection.settimeout(min(1, remaining))
                            connection.sendall(RESPONSE)
                except (TimeoutError, OSError):
                    pass


def main():
    parser = argparse.ArgumentParser(description="Check phone-to-spare reachability without an application or secrets.")
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=60)
    options = parser.parse_args()
    try:
        serve_marker(options.bind, options.port, options.seconds)
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError):
        parser.error("Use an available selected guest address, valid port, and bounded lifetime.")


if __name__ == "__main__":
    main()
