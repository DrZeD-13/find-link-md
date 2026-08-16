#!/usr/bin/env python3
"""
Link Checker for Markdown Files
Консольное приложение для проверки ссылок (http:// и file://) в .md файлах
"""

import sys
import os
import subprocess
import re
import argparse
from urllib.parse import urlparse, unquote
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Set

# Цвета для вывода
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'
BOLD = '\033[1m'


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


def find_md_files(folder_path: str) -> List[str]:
    """Поиск всех .md файлов в директории рекурсивно"""
    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"Путь не существует: {folder_path}")

    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"Указанный путь не является директорией: {folder_path}")

    try:
        # Используем find для поиска .md файлов
        cmd = ['find', folder_path, '-name', '*.md', '-type', 'f']
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        files = [f for f in result.stdout.strip().split('\n') if f]
        return files
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Ошибка при поиске файлов: {e.stderr}")
    except PermissionError as e:
        raise PermissionError(f"Нет прав доступа к директории: {e}")


def extract_links_from_files(folder_path: str) -> Dict[str, List[Tuple[str, str]]]:
    """
    Извлечение ссылок из .md файлов с помощью find + grep

    Returns:
        Dict[url, List[Tuple[file_path, line_number]]]
    """
    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"Путь не существует: {folder_path}")

    # Регулярное выражение для поиска ссылок в Markdown
    # Ищем [text](url) и ![alt](url)
    link_pattern = re.compile(r'[!]?\[[^\]]*\]\(([^)]+)\)')

    links_map = defaultdict(list)
    md_files = find_md_files(folder_path)

    if not md_files:
        raise ValueError("В указанной директории нет .md файлов")

    for file_path in md_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Находим все ссылки в файле
            matches = link_pattern.findall(content)

            # Проверяем каждую ссылку
            for match in matches:
                # Очищаем URL от тайтлов и лишних пробелов
                url = match.strip()
                # Удаляем возможный тайтл: "url "title""
                if ' ' in url and (url.startswith('"') or url.startswith("'")):
                    # Если URL содержит пробел и кавычки, это может быть тайтл
                    parts = url.split(' ', 1)
                    if parts[0].strip():
                        url = parts[0].strip()

                # Проверяем, что ссылка начинается с http://, https:// или file://
                if url.startswith(('http://', 'https://', 'file://')):
                    # Пропускаем относительные file:// ссылки
                    if url.startswith('file://') and url.count('/') < 3:
                        continue

                    # Нормализуем путь для file://
                    if url.startswith('file://'):
                        parsed = urlparse(url)
                        if parsed.path:
                            # Декодируем URL-кодированные символы
                            path = unquote(parsed.path)
                            # Нормализуем путь
                            if '..' in path or '//' in path:
                                path = os.path.normpath(path)
                            url = f'file://{path}'

                    links_map[url].append((file_path, 0))  # 0 - номер строки не определяем

        except (PermissionError, UnicodeDecodeError) as e:
            print(f"{RED}Ошибка при чтении файла {file_path}: {e}{RESET}", file=sys.stderr)
            continue

    return dict(links_map)


def check_file_link(link: str) -> Tuple[str, int, str]:
    """Проверка file:// ссылок"""
    parsed = urlparse(link)
    file_path = parsed.path

    if not file_path:
        return 'Error(404)', 404, 'Файл не найден'

    if os.path.exists(file_path):
        return 'Ok(200)', 200, 'Файл существует'
    else:
        return 'Error(404)', 404, 'Файл не найден'


def check_http_link(link: str, timeout: float = 1.0) -> Tuple[str, int, str]:
    """Проверка http:// и https:// ссылок"""
    try:
        response = requests.get(
            link,
            timeout=timeout,
            allow_redirects=False,
            headers={'User-Agent': 'LinkChecker/1.0'}
        )

        if 200 <= response.status_code < 300:
            return f'Ok({response.status_code})', response.status_code, 'Успешно'
        else:
            return f'Error({response.status_code})', response.status_code, f'HTTP {response.status_code}'

    except requests.exceptions.Timeout:
        return 'Error(Timeout)', 408, 'Таймаут'
    except requests.exceptions.ConnectionError:
        return 'Error(ConnectionError)', 0, 'Ошибка соединения'
    except requests.exceptions.RequestException as e:
        return f'Error({str(e)})', 0, str(e)


def check_link_status(link: str, timeout: float = 1.0) -> Tuple[str, int, str]:
    """Проверка статуса ссылки в зависимости от протокола"""
    if link.startswith('file://'):
        return check_file_link(link)
    elif link.startswith(('http://', 'https://')):
        return check_http_link(link, timeout)
    else:
        return 'Error(Unknown)', 0, 'Неизвестный протокол'


def display_results(results: Dict[str, List[Tuple[str, str, str, str]]], total_unique: int):
    """
    Отображение результатов в виде таблицы

    results: Dict[url, List[Tuple[status, status_code, message, file_path]]]
    """
    # Собираем все результаты для отображения
    all_results = []
    success_count = 0
    error_count = 0

    for url, entries in results.items():
        for status, status_code, message, file_path in entries:
            all_results.append((url, status, status_code, message, file_path))
            if status.startswith('Ok'):
                success_count += 1
            else:
                error_count += 1

    # Сортировка: сначала ошибки, потом успешные
    all_results.sort(key=lambda x: (x[1].startswith('Ok'), x[2]), reverse=False)

    # Вывод таблицы
    print(f"\n{BOLD}{'='*120}{RESET}")
    print(f"{BOLD}РЕЗУЛЬТАТЫ ПРОВЕРКИ ССЫЛОК{RESET}")
    print(f"{BOLD}{'='*120}{RESET}")

    # Заголовки
    print(f"{'Статус':<20} {'Ссылка':<60} {'Файл':<40}")
    print(f"{'-'*120}")

    for url, status, status_code, message, file_path in all_results:
        # Выбираем цвет
        if status.startswith('Ok'):
            color = GREEN
            icon = '✅'
        else:
            color = RED
            icon = '❌'

        # Сокращаем длинные ссылки
        display_url = url if len(url) <= 60 else url[:57] + '...'
        # Сокращаем длинные пути
        display_file = file_path if len(file_path) <= 40 else '...' + file_path[-37:]

        status_display = f"{color}{icon} {status}{RESET}"
        print(f"{status_display:<20} {display_url:<60} {display_file:<40}")

    print(f"{'-'*120}")

    # Итоговая статистика
    total = success_count + error_count
    print(f"\n{BOLD}Итого: {total}{RESET} "
          f"{GREEN}✅ Успешно: {success_count}{RESET} "
          f"{RED}❌ Ошибок: {error_count}{RESET}")
    print(f"{BOLD}Уникальных ссылок: {total_unique}{RESET}")


def main():
    """Главная функция программы"""
    try:
        args = parse_arguments()

        print(f"{BOLD}Сканирование директории: {args.path}{RESET}")

        # Извлекаем ссылки из файлов
        links_map = extract_links_from_files(args.path)

        if not links_map:
            print(f"{RED}Ссылок не найдено в .md файлах{RESET}")
            return 0

        total_unique = len(links_map)
        print(f"Найдено {total_unique} уникальных ссылок")

        # Проверяем статусы ссылок
        results = {}
        link_status_cache = {}

        # Используем ThreadPoolExecutor для параллельной проверки
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            future_to_link = {
                executor.submit(check_link_status, link, args.timeout): link
                for link in links_map.keys()
            }

            for future in as_completed(future_to_link):
                link = future_to_link[future]
                try:
                    status, status_code, message = future.result()
                    link_status_cache[link] = (status, status_code, message)
                except Exception as e:
                    link_status_cache[link] = (f'Error(Exception)', 0, str(e))

        # Формируем результаты для отображения
        for link, files in links_map.items():
            if link in link_status_cache:
                status, status_code, message = link_status_cache[link]
                results[link] = [
                    (status, status_code, message, file_path)
                    for file_path, _ in files
                ]
            else:
                results[link] = [
                    ('Error(Unknown)', 0, 'Неизвестная ошибка', file_path)
                    for file_path, _ in files
                ]

        # Отображаем результаты
        display_results(results, total_unique)

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