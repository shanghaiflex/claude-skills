# claude-skills

Скиллы для [Claude Code](https://claude.com/claude-code), которые работают с живыми сайтами через
настоящий Google Chrome под Playwright по CDP. У каждого скилла свой профиль Chrome, личный профиль
пользователя не затрагивается.

| Скилл | Что делает |
|---|---|
| [`avito`](avito/SKILL.md) | Поиск на Avito и чтение карточек объявлений; учитывает блок Qrator по IP и капчу |
| [`nalog`](nalog/SKILL.md) | 3-НДФЛ в личном кабинете ФНС: имущественный вычет, проценты по ипотеке, возврат налога |

Общее правило обоих: капчи, вход и всё, что сайт не даёт нажать программно, делает человек. Агент
читает, считает и заполняет текстовые поля.

## Установка

```sh
git clone https://github.com/shanghaiflex/claude-skills.git
cp -r claude-skills/avito claude-skills/nalog ~/.claude/skills/

python3 -m venv ~/.claude/tools/playwright/venv
~/.claude/tools/playwright/venv/bin/python -m pip install playwright pypdf pillow
```

Браузер Playwright скачивать не нужно: скиллы подключаются к установленному Google Chrome.
Дополнительные зависимости `nalog` (tesseract для сканов) описаны в [nalog/README.md](nalog/README.md).
