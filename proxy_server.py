import asyncio
import logging
from auth import check_auth

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9000

BUFFER = 65536
RATE_LIMIT = 1024 * 1024  # 1 MB/s

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


class RateLimiter:
    def __init__(self, rate):
        self.rate = rate
        self.tokens = rate
        self.updated = asyncio.get_event_loop().time()

    async def consume(self, amount):
        while True:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.updated
            self.updated = now

            self.tokens += elapsed * self.rate
            if self.tokens > self.rate:
                self.tokens = self.rate

            if self.tokens >= amount:
                self.tokens -= amount
                return

            await asyncio.sleep((amount - self.tokens) / self.rate)


async def pipe(reader, writer, limiter):
    try:
        while True:
            data = await reader.read(BUFFER)
            if not data:
                break

            await limiter.consume(len(data))

            writer.write(data)
            await writer.drain()
    except:
        pass
    finally:
        writer.close()


async def handle_connect(client_reader, client_writer, host, port):
    try:
        remote_reader, remote_writer = await asyncio.open_connection(host, port)

        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        limiter = RateLimiter(RATE_LIMIT)

        await asyncio.gather(
            pipe(client_reader, remote_writer, limiter),
            pipe(remote_reader, client_writer, limiter)
        )

    except Exception as e:
        logging.error(f"CONNECT error: {e}")


async def handle_http(client_reader, client_writer, first_line, headers, body):
    try:
        method, url, version = first_line.split()

        import urllib.parse
        parsed = urllib.parse.urlparse(url)

        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        remote_reader, remote_writer = await asyncio.open_connection(host, port)

        request = f"{method} {path} {version}\r\n".encode()

        for k, v in headers.items():
            if k.lower() != "proxy-authorization":
                request += f"{k}: {v}\r\n".encode()

        request += b"\r\n" + body

        remote_writer.write(request)
        await remote_writer.drain()

        limiter = RateLimiter(RATE_LIMIT)

        await asyncio.gather(
            pipe(remote_reader, client_writer, limiter),
            pipe(client_reader, remote_writer, limiter)
        )

    except Exception as e:
        logging.error(f"HTTP proxy error: {e}")


async def handle_client(reader, writer):
    try:
        data = await reader.readuntil(b"\r\n\r\n")

        header_block = data.decode(errors="ignore")
        lines = header_block.split("\r\n")

        first_line = lines[0]
        headers = {}

        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()

        if not check_auth(headers.get("Proxy-Authorization")):
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=\"proxy\"\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            return

        logging.info(f"Request: {first_line}")

        method = first_line.split()[0]

        if method == "CONNECT":
            host, port = first_line.split()[1].split(":")
            await handle_connect(reader, writer, host, int(port))
        else:
            body = b""
            await handle_http(reader, writer, first_line, headers, body)

    except Exception as e:
        logging.error(f"Client error: {e}")
    finally:
        try:
            writer.close()
        except:
            pass


async def main():
    server = await asyncio.start_server(
        handle_client,
        LISTEN_HOST,
        LISTEN_PORT
    )

    logging.info(f"Proxy started on {LISTEN_HOST}:{LISTEN_PORT}")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
