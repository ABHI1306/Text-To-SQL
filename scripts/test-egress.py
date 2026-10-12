import socket
import sys


def main() -> int:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            print("FAIL: application container can connect to a public IP")
            return 1
    except OSError as error:
        print(f"PASS: public egress is unavailable ({error.__class__.__name__})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
