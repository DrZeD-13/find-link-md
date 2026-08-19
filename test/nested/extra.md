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

Относительный file:// (относительно этого .md файла):
[relative](file://./relative.md)

Относительный file:// без точки:
[relative no dot](file://relative.md)

Относительный file:// в родительскую папку:
[relative parent](file://../test1.md)

Несуществующий относительный файл:
[relative missing](file://./no-such-file.md)

Несуществующий абсолютный файл:
[missing](file:///this/path/does/not/exist.txt)

Локальные пути без схемы file://:
[plain relative](relative.md)
[plain dot](./relative.md)
[plain parent](../test2.md)
[plain absolute](/etc/hosts)
[plain missing](./no-such-plain.md)

Ссылка с якорем (якорь отбрасывается при проверке):
[with anchor](relative.md#section)

Якорь внутри файла — не проверяем:
[anchor only](#some-section)

Другие схемы — не проверяем:
[mail](mailto:user@example.com)

Две ссылки в одной строке:
[first (одна)](relative.md) и [second (другая)](../test3.md)
