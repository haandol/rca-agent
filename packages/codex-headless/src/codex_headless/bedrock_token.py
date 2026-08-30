from __future__ import annotations

from aws_bedrock_token_generator import provide_token


def main() -> None:
    print(provide_token())


if __name__ == "__main__":
    main()
