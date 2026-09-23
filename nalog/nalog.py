#!/usr/bin/env python3
"""ЛК ФНС (lkfl2.nalog.ru) через реальный Chrome по CDP.

Агент читает и заполняет текстовые поля. Кнопки, чекбоксы и загрузку файлов
делает человек — программные клики ЛК перехватывает и при этом ломает форму.
"""
import argparse
import os
import re
import subprocess
import sys
import zipfile

PORT = int(os.environ.get("NL_PORT", "9222"))
PROFILE = os.path.expanduser("~/.claude/tools/playwright/chrome-profile")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN = "https://lkfl2.nalog.ru/lkfl/login"
DECLS = "https://lkfl2.nalog.ru/lkfl/individual/declarations"
DRAFT = "https://lkfl2.nalog.ru/lkfl/individual/appeals/3NDFL/3NDFL?cardId={}"

STEPS = ["Данные", "Доходы", "Выбор вычетов", "Вычеты", "Возврат переплаты",
         "Документы", "Подтверждение", "Отправка"]
MARKERS = {
    "Доходы": "Сведения об источнике дохода",
    "Выбор вычетов": "Выберите налоговые вычеты",
    "Вычеты": "Сведения об объектах",
    "Возврат переплаты": "Доступно к возврату",
    "Документы": "Прикрепление подтверждающих",
    "Подтверждение": "предварительного расчета",
}


def connect():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    return pw, browser


def lk_page(browser, create=False):
    pages = [p for c in browser.contexts for p in c.pages]
    hit = [p for p in pages if "lkfl2.nalog.ru" in p.url]
    if hit:
        hit[0].bring_to_front()
        return hit[0]
    if create:
        pg = browser.contexts[0].new_page()
        pg.goto(LOGIN, wait_until="domcontentloaded", timeout=60000)
        return pg
    sys.exit("нет вкладки ЛК — запусти `launch` и войди через Госуслуги")


def form(page):
    """Форма декларации живёт в безымянном iframe."""
    inner = [f for f in page.frames if f != page.main_frame]
    return inner[0] if inner else page.main_frame


def screen_text(page):
    return form(page).inner_text("body")


def current_step(text):
    for name, marker in MARKERS.items():
        if marker in text:
            return name
    return "?"


def cmd_launch(_):
    if subprocess.run(["curl", "-s", "--max-time", "3",
                       f"http://127.0.0.1:{PORT}/json/version"],
                      capture_output=True).returncode == 0:
        print(f"Chrome уже слушает CDP на {PORT}")
    else:
        os.makedirs(PROFILE, exist_ok=True)
        subprocess.Popen(
            [CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
             "--no-first-run", "--no-default-browser-check", LOGIN],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"Chrome поднят на профиле {PROFILE}")
    print("Вход через Госуслуги делает ПОЛЬЗОВАТЕЛЬ — капчи и пароли не наши.")


def cmd_state(_):
    pw, b = connect()
    pg = lk_page(b)
    top = pg.inner_text("body")[:400]
    if "Вход в личный кабинет" in top:
        print("СЕССИЯ ПРОТУХЛА — нужен вход через Госуслуги")
        if "ЕСИА) временно недоступен" in top:
            print("  ЕСИА лежит на стороне ФНС: ждать, повторы не помогут")
        pw.stop()
        return
    text = screen_text(pg)
    print("URL:", pg.url[:100])
    print("шаг:", current_step(text))
    for label, pattern in (("к возврату", r"(?:Доступно к возврату|сумма к возврату)[^\d]{0,40}([\d\s]+,\d\d)"),
                           ("вычеты", r"принимаемых в уменьшение доходов\s*([\d\s]+,\d\d)"),
                           ("лимит файлов", r"(Осталось [\d.]+ ?Мб из \d+ Мб)")):
        m = re.search(pattern, text)
        if m:
            print(f"{label}: {m.group(1).strip()}")
    pw.stop()


def cmd_dump(_):
    pw, b = connect()
    pg = lk_page(b)
    f = form(pg)
    print("=== ЭКРАН ===")
    print(f.inner_text("body")[:3000])
    print("=== ПОЛЯ ===")
    for el in f.query_selector_all("input, textarea"):
        if el.get_attribute("type") == "hidden":
            continue
        info = {k: el.get_attribute(k) for k in ("id", "type", "value", "aria-invalid", "disabled")}
        if info["type"] == "checkbox":
            info["checked"] = el.is_checked()
        print("  ", info)
    pw.stop()


def cmd_draft(args):
    pw, b = connect()
    pg = lk_page(b, create=True)
    pg.goto(DRAFT.format(args.cardId), wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(16000)
    if "Вход в личный кабинет" in pg.inner_text("body")[:300]:
        sys.exit("сессия протухла — пусть пользователь войдёт, потом повтори")
    for _ in range(len(STEPS)):
        text = screen_text(pg)
        step = current_step(text)
        print("шаг:", step)
        if args.to and step == args.to:
            break
        if step == "Подтверждение":
            break
        # чекбоксы не трогаем: клик по отмеченному снимает галочку и обнуляет вычет
        try:
            form(pg).click("button:has-text('Далее')", timeout=10000)
        except Exception as e:
            print("кнопка 'Далее' не поддалась — дальше вручную:", str(e)[:70])
            break
        pg.wait_for_timeout(13000)
    print("итоговый шаг:", current_step(screen_text(pg)))
    pw.stop()


def cmd_fill(args):
    pw, b = connect()
    pg = lk_page(b)
    f = form(pg)
    for el in f.query_selector_all("input"):
        if args.field in (el.get_attribute("id") or "") and not el.get_attribute("disabled"):
            el.fill(args.value)
            pg.wait_for_timeout(2500)
            print("заполнено:", el.get_attribute("id"), "=", el.get_attribute("value"))
            pw.stop()
            return
    print("поле не найдено; посмотри `dump`")
    pw.stop()


def cmd_receipt(args):
    """Чек ОФД по фискальным реквизитам из письма OFD.RU."""
    from pypdf import PdfReader, PdfWriter
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    url = f"https://check.ofd.ru/rec/{args.fn}/{args.fd}/{args.fp}"
    pw, b = connect()
    pg = b.contexts[0].new_page()
    pg.goto(url, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(4000)
    if "Кассовый чек" not in pg.inner_text("body"):
        pg.close(); pw.stop()
        sys.exit("чек не отдался — проверь ФН/ФД/ФПД")
    base = os.path.join(out, f"chek_{args.fd}")
    pg.screenshot(path=base + ".png", full_page=True)
    try:
        with pg.expect_download(timeout=25000) as dl:
            pg.click("text=Скачать PDF")
        raw = base + "_raw.pdf"
        dl.value.save_as(raw)
        r = PdfReader(raw)
        if r.is_encrypted:           # ЛК не принимает зашифрованные: "файл повреждён или защищён"
            r.decrypt("")
        w = PdfWriter()
        for p in r.pages:
            w.add_page(p)
        with open(base + ".pdf", "wb") as fh:
            w.write(fh)
        os.remove(raw)
        print("PDF:", base + ".pdf")
    except Exception as e:
        print("PDF не скачался, остаётся PNG:", str(e)[:70])
    print("PNG:", base + ".png")
    pg.close()
    pw.stop()


def cmd_ocr(args):
    from pypdf import PdfReader
    reader = PdfReader(args.pdf)
    first, last = 1, len(reader.pages)
    if args.pages:
        part = args.pages.split("-")
        first, last = int(part[0]), int(part[-1])
    tmp = "/tmp/nalog_ocr.png"
    for n in range(first - 1, min(last, len(reader.pages))):
        page = reader.pages[n]
        text = (page.extract_text() or "").strip()
        if text:
            print(f"--- стр.{n+1} (текстовый слой) ---")
            print(" ".join(text.split())[:1500])
            continue
        images = list(page.images)
        if not images:
            print(f"--- стр.{n+1}: ни текста, ни изображений")
            continue
        with open(tmp, "wb") as fh:
            fh.write(images[0].data)
        res = subprocess.run(["tesseract", tmp, "stdout", "-l", "rus", "--psm", "6"],
                             capture_output=True, text=True)
        print(f"--- стр.{n+1} (OCR) ---")
        print(" ".join(res.stdout.split())[:1500])


def cmd_unzip(args):
    """Пакет Росреестра: имена внутри в битой кодировке, опознаём по содержимому."""
    from pypdf import PdfReader
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    zf = zipfile.ZipFile(args.zip)
    n = 0
    for info in zf.infolist():
        if not info.filename.lower().endswith(".pdf"):
            continue
        n += 1
        path = os.path.join(out, f"doc{n}.pdf")
        with open(path, "wb") as fh:
            fh.write(zf.read(info))
        try:
            head = " ".join((PdfReader(path).pages[0].extract_text() or "").split())[:160]
        except Exception as e:
            head = f"(не прочитать: {e})"
        print(f"doc{n}.pdf  {os.path.getsize(path):>9} б  {head or '(скан без текста — читай `ocr`)'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("launch").set_defaults(handler=cmd_launch)
    sub.add_parser("state").set_defaults(handler=cmd_state)
    sub.add_parser("dump").set_defaults(handler=cmd_dump)

    p = sub.add_parser("draft"); p.add_argument("cardId")
    p.add_argument("--to", choices=list(MARKERS)); p.set_defaults(handler=cmd_draft)

    p = sub.add_parser("fill"); p.add_argument("field"); p.add_argument("value")
    p.set_defaults(handler=cmd_fill)

    p = sub.add_parser("receipt"); p.add_argument("fn"); p.add_argument("fd"); p.add_argument("fp")
    p.add_argument("--out", default="."); p.set_defaults(handler=cmd_receipt)

    p = sub.add_parser("ocr"); p.add_argument("pdf"); p.add_argument("--pages")
    p.set_defaults(handler=cmd_ocr)

    p = sub.add_parser("unzip"); p.add_argument("zip"); p.add_argument("--out", default=".")
    p.set_defaults(handler=cmd_unzip)

    args = ap.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
