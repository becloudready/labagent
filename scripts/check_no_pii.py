#!/usr/bin/env python3
"""Fail if tracked files contain what must never be committed: real emails, AWS account IDs, keys.

Run in CI and before every push. Allowed: example.com / example.org placeholders,
and the documentation account 123456789012.
"""
import re
import subprocess
import sys

ALLOWED_EMAIL = re.compile(r"@(example\.(com|org)|users\.noreply\.github\.com)$", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCOUNT = re.compile(r"(?<![\d])(?!123456789012)\d{12}(?![\d])")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
KEYS = re.compile(r"AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC )?PRIVATE KEY-----|aws_secret_access_key\s*=\s*\S{20,}", re.I)
SKIP = ("LICENSE", ".venv/")


def main() -> int:
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.split()
    bad = []
    for f in files:
        if f.startswith(SKIP) or f in SKIP or f.endswith((".png", ".gif", ".jpg")):
            continue
        try:
            text = open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in EMAIL.finditer(line):
                if not ALLOWED_EMAIL.search(m.group(0)):
                    bad.append(f"{f}:{n}: email")
            line = UUID.sub("<uuid>", line)
            if any(not re.fullmatch(r"(\d)\1{11}", m.group(0)) for m in ACCOUNT.finditer(line)) and "arn:" not in line.lower().replace("arn:aws:iam::aws", ""):
                # bare 12-digit numbers outside ARNs are still suspicious
                bad.append(f"{f}:{n}: 12-digit number (AWS account id?)")
            elif re.search(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(?!123456789012|\*|\{)\d{12}:", line):
                bad.append(f"{f}:{n}: ARN with a real account id")
            if KEYS.search(line):
                bad.append(f"{f}:{n}: key/secret pattern")
    for b in bad:
        print(b, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
