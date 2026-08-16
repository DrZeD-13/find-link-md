#!/usr/bin/env python3
"""
Link Checker for Markdown Files
Консольное приложение для проверки ссылок (http:// и file://) в .md файлах
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple
from urllib.parse import unquote, urlparse

GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'
BOLD = '\033[1m'

# find + grep: markdown [text](url) / ![alt](url), без кавычек title
GREP_LINK_PATTERN = r'\[[^]]*\]\((https?://[^)"[:space:]]+|file://[^)"[:space:]]+)'


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Возвращает статус текущего URL без перехода по редиректу."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_arguments():
    """Парсинг аргументов командной строки"""
    parser = argparse.ArgumentParser(
        description='Проверка ссылок в Markdown файлах'
    )
    parser.add_argument(
        'path',
        help='Путь к корневой папке для сканирования'
    )
    parser.add_argument(
        '--timeout',
        type=float,
        default=1.0,
        help='Таймаут для HTTP запросов (по умолчанию: 1.0 сек)'
    )
    parser.add_argument(
        '--max-workers',
        type=int,
        default=10,
        help='Максимальное количество параллельных HTTP запросов (по умолчанию: 10)'
    )
    return parser.parse_args()


def _ensure_folder(folder_path: str) -> str:
    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"Путь не существует: {folder_path}")
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"Указанный путь не является директорией: {folder_path}")
    return os.path.abspath(folder_path)


def find_md_files(folder_path: str) -> List[str]:
    """Поиск всех .md файлов через find (без os.walk)."""
    folder_path = _ensure_folder(folder_path)
    try:
        result = subprocess.run(
            ['find', folder_path, '-name', '*.md', '-type', 'f'],
            capture_output=True,
            text=True,
            check=False,
        )
    except PermissionError as e:
        raise PermissionError(f"Нет прав доступа к директории: {e}") from e

    if result.returncode != 0:
        err = (result.stderr or '').strip() or 'неизвестная ошибка find'
        raise RuntimeError(f"Ошибка при поиске файлов: {err}")

    return [line for line in result.stdout.splitlines() if line]


def _normalize_file_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.path:
        return url
    path = os.path.normpath(unquote(parsed.path))
    if not path.startswith('/'):
        path = '/' + path
    return f'file://{path}'


def _is_absolute_file_url(url: str) -> bool:
    """Абсолютные file:///path; относительные file://./... пропускаем."""
    if not url.startswith('file://'):
        return False
    return url.startswith('file:///')


def extract_links_from_files(folder_path: str) -> Dict[str, List[str]]:
    """
    Извлечение ссылок из .md файлов с помощью find + grep.

    Returns:
        Dict[url, List[file_path]]
    """
    folder_path = _ensure_folder(folder_path)
    md_files = find_md_files(folder_path)
    if not md_files:
        raise ValueError("В указанной директории нет .md файлов")

    try:
        result = subprocess.run(
            [
                'find', folder_path, '-name', '*.md', '-type', 'f',
                '-exec', 'grep', '-Hn', '-o', '-E', GREP_LINK_PATTERN, '{}', ';',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except PermissionError as e:
        raise PermissionError(f"Нет прав доступа к файлам: {e}") from e

    links_map: Dict[str, List[str]] = defaultdict(list)
    seen = set()
    url_re = re.compile(r'\[[^\]]*\]\((https?://[^)"\s]+|file://[^)"\s]+)')

    for raw_line in result.stdout.splitlines():
        # path:line:match — путь может содержать ':'
        parts = raw_line.split(':', 2)
        if len(parts) < 3:
            continue
        file_path, _line_no, match = parts
        url_match = url_re.search(match)
        if not url_match:
            continue
        url = url_match.group(1).strip()

        if url.startswith('file://'):
            if not _is_absolute_file_url(url):
                continue
            url = _normalize_file_url(url)
        elif not url.startswith(('http://', 'https://')):
            continue

        key = (url, file_path)
        if key in seen:
            continue
        seen.add(key)
        links_map[url].append(file_path)

    return dict(links_map)


def check_file_link(link: str) -> Tuple[str, int, str]:
    """Проверка file:// ссылок"""
    parsed = urlparse(link)
    file_path = unquote(parsed.path)

    if not file_path or not os.path.exists(file_path):
        return 'Error(404)', 404, 'Файл не найден'
    return 'Ok(200)', 200, 'Файл существует'


def check_http_link(link: str, timeout: float = 1.0) -> Tuple[str, int, str]:
    """Проверка http:// и https:// ссылок без следования редиректам."""
    request = urllib.request.Request(
        link,
        method='GET',
        headers={'User-Agent': 'LinkChecker/1.0'},
    )
    opener = urllib.request.build_opener(NoRedirectHandler)

    try:
        with opener.open(request, timeout=timeout) as response:
            code = response.getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    except socket.timeout:
        return 'Error(Timeout)', 408, 'Таймаут'
    except TimeoutError:
        return 'Error(Timeout)', 408, 'Таймаут'
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return 'Error(Timeout)', 408, 'Таймаут'
        return 'Error(ConnectionError)', 0, 'Ошибка соединения'
    except Exception as e:
        return f'Error({e})', 0, str(e)

    if 200 <= code < 300:
        return f'Ok({code})', code, 'Успешно'
    return f'Error({code})', code, f'HTTP {code}'


def check_link_status(link: str, base_path: str, timeout: float = 1.0) -> Tuple[str, int, str]:
    """Проверка статуса ссылки в зависимости от протокола"""
    del base_path  # абсолютные file://, корень сканирования не нужен
    if link.startswith('file://'):
        return check_file_link(link)
    if link.startswith(('http://', 'https://')):
        return check_http_link(link, timeout)
    return 'Error(Unknown)', 0, 'Неизвестный протокол'


def display_results(
    results: Dict[str, List[Tuple[str, int, str, str]]],
    total_unique: int,
    folder_path: str,
) -> None:
    """Отображение результатов в виде таблицы."""
    all_results = []
    unique_status = {}

    for url, entries in results.items():
        if not entries:
            continue
        unique_status[url] = entries[0][0]
        for status, status_code, message, file_path in entries:
            rel_file = os.path.relpath(file_path, folder_path)
            all_results.append((url, status, status_code, message, rel_file))

    all_results.sort(key=lambda x: (x[1].startswith('Ok'), x[0]))

    success_count = sum(1 for status in unique_status.values() if status.startswith('Ok'))
    error_count = total_unique - success_count

    print(f"\n{BOLD}{'=' * 120}{RESET}")
    print(f"{BOLD}РЕЗУЛЬТАТЫ ПРОВЕРКИ ССЫЛОК{RESET}")
    print(f"{BOLD}{'=' * 120}{RESET}")
    print(f"{'Статус':<28} {'Ссылка':<56} {'Файл':<34}")
    print(f"{'-' * 120}")

    for url, status, status_code, message, file_path in all_results:
        if status.startswith('Ok'):
            color = GREEN
            icon = '✅'
        else:
            color = RED
            icon = '❌'

        display_url = url if len(url) <= 56 else url[:53] + '...'
        display_file = file_path if len(file_path) <= 34 else '...' + file_path[-31:]
        status_plain = f"{icon} {status}"
        print(f"{color}{status_plain:<28}{RESET} {display_url:<56} {display_file:<34}")

    print(f"{'-' * 120}")
    print(
        f"\n{BOLD}Итого: {total_unique}{RESET} "
        f"{GREEN}✅ Успешно: {success_count}{RESET} "
        f"{RED}❌ Ошибок: {error_count}{RESET}"
    )


def main():
    """Главная функция программы"""
    try:
        if sys.platform != 'darwin':
            print(f"{RED}Ошибка: приложение поддерживается только на macOS{RESET}", file=sys.stderr)
            return 1

        args = parse_arguments()
        folder_path = os.path.abspath(args.path)
        _ensure_folder(folder_path)

        print(f"{BOLD}Сканирование директории: {folder_path}{RESET}")

        links_map = extract_links_from_files(folder_path)
        if not links_map:
            print(f"{RED}Ссылок не найдено в .md файлах{RESET}")
            return 0

        total_unique = len(links_map)
        print(f"Найдено {total_unique} уникальных ссылок")

        link_status_cache = {}
        workers = max(1, min(args.max_workers, 10, total_unique))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_link = {
                executor.submit(check_link_status, link, folder_path, args.timeout): link
                for link in links_map
            }
            for future in as_completed(future_to_link):
                link = future_to_link[future]
                try:
                    link_status_cache[link] = future.result()
                except Exception as e:
                    link_status_cache[link] = ('Error(Exception)', 0, str(e))

        results = {}
        for link, files in links_map.items():
            status, status_code, message = link_status_cache.get(
                link, ('Error(Unknown)', 0, 'Неизвестная ошибка')
            )
            results[link] = [
                (status, status_code, message, file_path)
                for file_path in files
            ]

        display_results(results, total_unique, folder_path)
        return 0

    except FileNotFoundError as e:
        print(f"{RED}Ошибка: {e}{RESET}", file=sys.stderr)
        return 1
    except NotADirectoryError as e:
        print(f"{RED}Ошибка: {e}{RESET}", file=sys.stderr)
        return 1
    except PermissionError as e:
        print(f"{RED}Ошибка доступа: {e}{RESET}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"{RED}Ошибка: {e}{RESET}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"{RED}Неожиданная ошибка: {e}{RESET}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
