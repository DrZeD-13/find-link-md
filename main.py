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

__version__ = '1.1'

GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'
BOLD = '\033[1m'

# grep лишь отбирает строки-кандидаты по фиксированной подстроке "](",
# чтобы поведение не зависело от реализации grep (GNU на Linux, BSD на macOS);
# сам разбор markdown-ссылок [text](target) / ![alt](target) делает Python
GREP_LINE_MARKER = ']('

# markdown [text](target), target без пробелов и кавычек (title отсекается)
_LINK_RE = re.compile(r'\[[^\]]*\]\(([^)"\s]+)')

# mailto:, ftp:, tel: и прочие схемы, которые не проверяем
_SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*:')


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
    parser.add_argument(
        '--version',
        action='version',
        version=f'link_checker {__version__}'
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


def _has_utf16_bom(file_path: str) -> bool:
    try:
        with open(file_path, 'rb') as fh:
            return fh.read(2) in (b'\xff\xfe', b'\xfe\xff')
    except OSError:
        return False


def _resolve_local_url(target: str, source_file: str) -> str:
    """
    Приведение локальной ссылки к абсолютному виду file:///path.

    Поддерживаются оба формата записи: со схемой (file:///abs, file://./rel)
    и обычным путём (/abs, ./rel, ../rel, sub/file.md). Относительные пути
    разрешаются относительно папки .md файла, в котором найдена ссылка.
    Якорь (#section) при проверке существования файла отбрасывается.
    """
    if target.startswith('file://'):
        target = target[len('file://'):]
    target = target.split('#', 1)[0]
    if not target:
        return ''
    raw_path = unquote(target)
    if not raw_path.startswith('/'):
        raw_path = os.path.join(os.path.dirname(source_file), raw_path)
    return f'file://{os.path.normpath(raw_path)}'


def extract_links_from_files(folder_path: str) -> Dict[str, List[str]]:
    """
    Извлечение ссылок из .md файлов с помощью find + grep.

    Returns:
        Dict[url, List[file_path]]

    url — ссылка для проверки и вывода: http(s) как записана,
    локальные приводятся к абсолютному виду file:///path.
    """
    folder_path = _ensure_folder(folder_path)
    md_files = find_md_files(folder_path)
    if not md_files:
        raise ValueError("В указанной директории нет .md файлов")

    try:
        # -a: файлы с бинарными байтами не пропускать молча
        result = subprocess.run(
            [
                'find', folder_path, '-name', '*.md', '-type', 'f',
                '-exec', 'grep', '-Hn', '-a', '-F', GREP_LINE_MARKER, '{}', ';',
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
        )
    except PermissionError as e:
        raise PermissionError(f"Нет прав доступа к файлам: {e}") from e

    links_map: Dict[str, List[str]] = defaultdict(list)
    seen = set()

    def add_links_from_line(line: str, file_path: str) -> None:
        for url_match in _LINK_RE.finditer(line):
            url = url_match.group(1).strip()

            if url.startswith(('http://', 'https://')):
                check_url = url
            elif url.startswith('#'):
                continue  # якорь внутри текущего файла
            elif _SCHEME_RE.match(url) and not url.startswith('file://'):
                continue  # mailto:, ftp: и другие схемы
            else:
                check_url = _resolve_local_url(url, file_path)
                if not check_url:
                    continue

            key = (check_url, file_path)
            if key in seen:
                continue
            seen.add(key)
            links_map[check_url].append(file_path)

    for raw_line in result.stdout.splitlines():
        # path:line:содержимое строки — путь может содержать ':'
        parts = raw_line.split(':', 2)
        if len(parts) < 3:
            continue
        file_path, _line_no, line = parts
        add_links_from_line(line, file_path)

    # UTF-16 файлы (с BOM) grep не видит: подстрока "](" в них разбита
    # нулевыми байтами, поэтому такие файлы разбираем напрямую в Python
    for file_path in md_files:
        if not _has_utf16_bom(file_path):
            continue
        try:
            with open(file_path, encoding='utf-16', errors='replace') as fh:
                for line in fh:
                    add_links_from_line(line, file_path)
        except OSError:
            continue

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

    if 200 <= code < 400:
        return f'Ok({code})', code, 'Успешно'
    return f'Error({code})', code, f'HTTP {code}'


def check_link_status(link: str, base_path: str, timeout: float = 1.0) -> Tuple[str, int, str]:
    """Проверка статуса ссылки в зависимости от протокола"""
    del base_path  # file:// уже приведены к абсолютным при извлечении
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
        args = parse_arguments()

        if sys.platform != 'darwin':
            print(f"{RED}Ошибка: приложение поддерживается только на macOS{RESET}", file=sys.stderr)
            return 1

        folder_path = os.path.abspath(args.path)
        _ensure_folder(folder_path)

        print(f"{BOLD}link_checker {__version__} — сканирование директории: {folder_path}{RESET}")

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
