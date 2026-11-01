import argparse
import http.client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--path",
        choices=("/health/live", "/health/ready"),
        default="/health/ready",
    )
    arguments = parser.parse_args()

    connection = http.client.HTTPConnection(arguments.host, arguments.port, timeout=2)
    try:
        connection.request("GET", arguments.path, headers={"Connection": "close"})
        response = connection.getresponse()
        response.read(1025)
        return 0 if response.status == 200 else 1
    except (OSError, http.client.HTTPException, ValueError):
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
