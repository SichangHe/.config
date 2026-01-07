#!/usr/bin/env python3
from requests import get

AGENTS_MD_URL = "https://raw.githubusercontent.com/SichangHe/sichanghe.github.io/refs/heads/main/src/notes/automation_software/AGENTS.md"


def main():
    response = get(AGENTS_MD_URL)
    if response.status_code == 200:
        print(response.text)
    else:
        print(f"Failed to fetch AGENTS.md. Report to the user: {response}")


main() if __name__ == "__main__" else None
