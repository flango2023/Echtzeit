"""Start the API with safe defaults.

    python -m echtzeit.api.serve --keys keys.json --db sqlite:///echtzeit.db
    python -m echtzeit.api.serve --keys keys.json --db sqlite:///echtzeit.db \\
        --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem --db-key-file db.key

Rules:

* Without TLS the server binds only to a loopback address (127.0.0.1, ::1 or localhost).
  Any other address needs a certificate and a private key, so keys never cross a network in
  clear text.
* Any other address also needs --db-key-file, so the database is encrypted at rest
  (see echtzeit.dbkey). On loopback the key file is optional.
* The private key file must not be readable by group or others.
* TLS uses Python's default server context, which refuses protocol versions below TLS 1.2.
* Over HTTPS every response carries Strict-Transport-Security.
"""

import argparse
import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from echtzeit.dbkey import DbKeyError, load_key_file

LOOPBACK_NAMES = {"localhost"}


class ServeConfigError(Exception):
    """The requested server settings are unsafe or incomplete."""


@dataclass(frozen=True)
class ServeConfig:
    host: str
    port: int
    tls_cert: Path | None
    tls_key: Path | None
    db_key_file: Path | None = None

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_config(
    host: str,
    port: int,
    tls_cert: Path | None,
    tls_key: Path | None,
    db_key_file: Path | None = None,
) -> ServeConfig:
    if not 1 <= port <= 65535:
        raise ServeConfigError("port must be between 1 and 65535")
    if (tls_cert is None) != (tls_key is None):
        raise ServeConfigError("give both --tls-cert and --tls-key, or neither")
    if tls_cert is None and not is_loopback(host):
        raise ServeConfigError(
            f"refusing to listen on {host} without TLS; use a loopback address or give "
            "--tls-cert and --tls-key"
        )
    if tls_cert is not None:
        for label, path in (("certificate", tls_cert), ("private key", tls_key)):
            if not path.is_file():
                raise ServeConfigError(f"TLS {label} {path} does not exist")
        if tls_key.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ServeConfigError(
                f"TLS private key {tls_key} is accessible by group or others; "
                f"run: chmod 600 {tls_key}"
            )
    if db_key_file is None and not is_loopback(host):
        raise ServeConfigError(
            f"refusing to listen on {host} with an unencrypted database; give --db-key-file "
            "(create one with: python -m echtzeit.dbkey new db.key)"
        )
    if db_key_file is not None:
        try:
            load_key_file(db_key_file)
        except DbKeyError as exc:
            raise ServeConfigError(str(exc)) from None
    return ServeConfig(host, port, tls_cert, tls_key, db_key_file)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start the Echtzeit API.")
    parser.add_argument("--keys", required=True, type=Path, help="API key file (mode 600)")
    parser.add_argument("--db", default="sqlite:///echtzeit.db", help="database URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--db-key-file", type=Path, help="database encryption key file (mode 600)")
    args = parser.parse_args(argv)
    try:
        config = check_config(args.host, args.port, args.tls_cert, args.tls_key, args.db_key_file)
    except ServeConfigError as exc:
        parser.error(str(exc))

    import uvicorn  # imported late so the checks above work without the api extra

    os.environ["ECHTZEIT_API_KEYS_FILE"] = str(args.keys)
    os.environ["ECHTZEIT_DATABASE_URL"] = args.db
    if config.db_key_file is not None:
        os.environ["ECHTZEIT_DB_KEY_FILE"] = str(config.db_key_file)
    uvicorn.run(
        "echtzeit.api.app:create_app_from_env",
        factory=True,
        host=config.host,
        port=config.port,
        ssl_certfile=str(config.tls_cert) if config.tls else None,
        ssl_keyfile=str(config.tls_key) if config.tls else None,
        server_header=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
