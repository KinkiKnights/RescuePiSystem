"""自己署名証明書を生成する（LAN内 HTTPS / iPhone のマイク利用向け）。

iOS Safari は localhost 以外ではマイク利用に HTTPS（セキュアコンテキスト）が
必須なので、操作画面サーバー(8765)と音声サーバー(8766)を HTTPS/WSS で起動する
ための証明書を作る。

使い方:
    pip install cryptography
    python make_cert.py                # localhost + 自動検出した LAN IP を SAN に含める
    python make_cert.py 192.168.1.50 mypc.local   # 追加のIP/ホスト名を指定

出力: certs/cert.pem, certs/key.pem
  → そのまま `python main.py` / `python voice_comm/server.py` を起動すると HTTPS になる。

iPhone 側:
  - https://<このPCのIP>:8765/reporter を開き、証明書の警告を許可（または
    cert.pem を構成プロファイルとしてインストールして信頼）すると、マイクが使える。
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import sys
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    print("cryptography が必要です:  pip install cryptography")
    sys.exit(1)


def main() -> None:
    cert_dir = Path(__file__).resolve().parent / "certs"
    cert_dir.mkdir(exist_ok=True)

    dns_names = ["localhost"]
    ip_addrs = ["127.0.0.1"]

    # ローカルの LAN IP を自動検出
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip and ip not in ip_addrs:
                ip_addrs.append(ip)
    except Exception:
        pass

    # 追加の IP / ホスト名（コマンドライン引数）
    for arg in sys.argv[1:]:
        try:
            ipaddress.ip_address(arg)
            if arg not in ip_addrs:
                ip_addrs.append(arg)
        except ValueError:
            if arg not in dns_names:
                dns_names.append(arg)

    san = [x509.DNSName(d) for d in dns_names]
    san += [x509.IPAddress(ipaddress.ip_address(i)) for i in ip_addrs]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rescue-dummy")])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )

    (cert_dir / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (cert_dir / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print("生成しました: certs/cert.pem, certs/key.pem")
    print("  DNS:", ", ".join(dns_names))
    print("  IP :", ", ".join(ip_addrs))
    print("次に `python main.py` を起動すると HTTPS になります。")


if __name__ == "__main__":
    main()
