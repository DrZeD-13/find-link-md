# Nested

Повтор Google, статус должен запроситься один раз:
[Google again](https://google.com)

Картинка:
![logo](https://example.com)

Ссылка с тайтлом:
[Example](https://example.com "Example title")

HTML не должен попасть в отчёт:
<a href="https://should-not-be-checked.example">html</a>

Reference-style тоже игнорируем:
[ref link][refid]
[refid]: https://reference-style.example

Относительный file:// пропускаем:
[relative](file://./relative.md)

Несуществующий абсолютный файл:
[missing](file:///this/path/does/not/exist.txt)
