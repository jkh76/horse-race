"""경마 레이스 자동 점검

실행 방법 (저장소 맨 위 폴더에서):
    pip install playwright
    playwright install chromium
    python3 tests/run_tests.py

폰·태블릿·큰 PC 화면 크기에서 실제로 경주를 끝까지 돌려 보고 아래를 확인한다.
  - 페이지 오류가 없는지
  - 순위표에 참가자가 한 번씩, 1위부터 기록 시간 순서로 나오는지
  - 초시계·시상대·결승 판독 사진·경주로 이름표의 순위와 기록이 순위표와 같은지
  - 막판 슬로모션과 줌이 일어나는지
  - 다크호스 수가 거리별로 맞는지 (500m 1, 750m 2, 1,000m 3마리, 단 '말 수 − 3'마리까지)
  - 화면 켜짐 유지가 경주 시작 때 켜지고 시상식 뒤에 꺼지는지
  - 이름 길이 제한(한글 3글자, 영문 6글자)과 8명 제한이 지켜지는지
  - 맨 아래 버전 표시가 있는지
"""
import asyncio
import pathlib
import re
import sys

from playwright.async_api import async_playwright

PAGE = (pathlib.Path(__file__).resolve().parent.parent / "index.html").as_uri()

SIZES = {
    "폰": dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2),
    "태블릿": dict(viewport={"width": 800, "height": 1280}, is_mobile=True, has_touch=True, device_scale_factor=2),
    "PC": dict(viewport={"width": 1920, "height": 1080}),
}
DISTANCES = {"10": ("500m", 1), "15": ("750m", 2), "20": ("1,000m", 3)}

# (화면, 경주 시간 값, 말 수): 다크호스 수는 거리별 수와 '말 수 − 3' 중 작은 쪽
CASES = [
    ("폰", "10", 8), ("태블릿", "15", 8), ("PC", "20", 8),
    ("폰", "20", 4), ("태블릿", "20", 5), ("PC", "15", 4),
    ("폰", "15", 5), ("태블릿", "10", 6), ("PC", "20", 6),
]

# 화면 켜짐 유지 기능을 흉내 내어 켜고 끈 기록을 남김
WAKE_MOCK = """
window.__wake = [];
Object.defineProperty(navigator, 'wakeLock', { value: { request: async () => {
  window.__wake.push('켜짐');
  const lock = { _f: [], addEventListener(e, f) { this._f.push(f); },
    release: async function () { window.__wake.push('꺼짐'); this._f.forEach(f => f()); } };
  return lock; } } });
"""
# 점검 시간을 줄이려고 애니메이션 시계를 3배 빠르게 (경주 계산 방식은 그대로)
FAST_CLOCK = """
(() => { const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = cb => raf(ts => cb(ts * 3)); })();
"""

# 경주가 끝난 화면에서 순위표·초시계·시상대·판독 사진·이름표를 모두 읽어 옴
READ_RESULT = """() => {
  const rows = [...document.querySelectorAll('#results .res')].map(li => ({
    rank: li.querySelector('.rankpill').textContent.replace(/[^0-9]/g, ''),
    name: li.querySelector('.rname').textContent,
    time: parseFloat(li.querySelector('.rtime').textContent) }));
  const podium = {};
  document.querySelectorAll('#podium li').forEach(li => { podium[li.className] = li.querySelector('.pname').textContent; });
  const tags = {};
  document.querySelectorAll('#lanes .tag').forEach(t => { tags[t.querySelector('span').textContent] = t.querySelector('.rk').textContent; });
  return {
    rows, podium, tags,
    roster: [...document.querySelectorAll('#roster .cname')].map(e => e.textContent),
    clock: parseFloat(document.getElementById('clock').textContent),
    photoNames: [...document.querySelectorAll('#pfoto .pftag')].map(e => e.textContent),
    photoTimes: [...document.querySelectorAll('#pfoto .pftime')].map(e => parseFloat(e.textContent)),
    photoGap: parseFloat((document.querySelector('#pfoto .pfmargin') || {}).textContent || 'NaN') };
}"""

failures = []


def check(ok, label):
    print(("  ✅ " if ok else "  ❌ ") + label)
    if not ok:
        failures.append(label)


async def race(browser, size_name, dur, n):
    dist, by_dist = DISTANCES[dur]
    want_boosts = min(by_dist, n - 3)
    ctx = await browser.new_context(**SIZES[size_name])
    await ctx.add_init_script(WAKE_MOCK)
    await ctx.add_init_script(FAST_CLOCK)
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(PAGE)
    await page.wait_for_timeout(300)

    for _ in range(n):
        await page.click("#sampleBtn")
    await page.click(f'input[name=dur][value="{dur}"] + span')
    await page.click("#dhBtn")
    await page.click("#startBtn")

    boosted, saw_slow, saw_zoom = set(), False, False
    for _ in range((int(dur) + 12) * 10):
        state = await page.evaluate("""() => ({
            boost: [...document.querySelectorAll('#lanes .horse')].map((h, i) => h.classList.contains('boost') ? i : -1).filter(i => i >= 0),
            slow: document.getElementById('stage').classList.contains('slow'),
            zoom: !!document.getElementById('lanes').style.transform,
            full: document.getElementById('arena').classList.contains('full'),
            done: document.querySelectorAll('#results .rname').length })""")
        boosted.update(state["boost"])
        saw_slow |= state["slow"]
        saw_zoom |= state["zoom"]
        if state["done"] == n and not state["full"]:
            break
        await page.wait_for_timeout(100)

    r = await page.evaluate(READ_RESULT)
    wake = await page.evaluate("window.__wake")
    rows = r["rows"]
    names = [x["name"] for x in rows]
    times = [x["time"] for x in rows]
    print(f"[{size_name} · {dist} · {n}마리]")
    check(not errors, f"페이지 오류 없음 {errors if errors else ''}")
    check(len(rows) == n and sorted(names) == sorted(r["roster"]),
          f"순위표에 참가자 {n}명이 한 번씩 (실제 {len(rows)}명)")
    check([x["rank"] for x in rows] == [str(i + 1) for i in range(n)], "순위 번호가 1위부터 차례대로")
    check(all(a <= b for a, b in zip(times, times[1:])), f"순위표가 기록 시간 순서 {times}")
    check(abs(r["clock"] - times[0]) < 0.006, f"초시계가 1위 기록에서 멈춤 ({r['clock']} / {times[0]})")
    want_podium = {"p%d" % (i + 1): names[i] for i in range(min(3, n))}
    check(r["podium"] == want_podium, f"시상대 = 순위표 1~{min(3, n)}위")
    check(r["photoNames"] == names[:2] and r["photoTimes"] == times[:2],
          "판독 사진 = 순위표 1·2위 이름과 기록")
    check(abs(r["photoGap"] - (times[1] - times[0])) < 0.011, f"판독 사진 기록 차 ({r['photoGap']}초)")
    check(all(r["tags"].get(nm) == f"{i + 1}위" for i, nm in enumerate(names)), "경주로 이름표 순위 = 순위표")
    check(saw_slow and saw_zoom, "막판 슬로모션과 줌")
    check(len(boosted) == want_boosts, f"다크호스 {want_boosts}마리 (실제 {len(boosted)}마리)")
    check(wake == ["켜짐", "꺼짐"], f"화면 켜짐 유지: 시작 때 켜지고 끝나면 꺼짐 (기록 {wake})")
    await ctx.close()


async def input_rules(browser):
    ctx = await browser.new_context(**SIZES["폰"])
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(PAGE)
    await page.wait_for_timeout(300)
    print("[입력 규칙]")
    await page.fill("#nameInput", "가나다라")
    await page.click("#addBtn")
    await page.fill("#nameInput", "ABCDEFGH")
    await page.click("#addBtn")
    names = await page.evaluate("[...document.querySelectorAll('#roster .cname')].map(e => e.textContent)")
    check(names == ["가나다", "ABCDEF"], f"이름 길이 제한 (실제 {names})")
    for _ in range(6):
        await page.click("#sampleBtn")
    count = await page.evaluate("document.querySelectorAll('#roster .chip').length")
    locked = await page.evaluate("['nameInput', 'addBtn', 'sampleBtn'].every(id => document.getElementById(id).disabled)")
    check(count == 8 and locked, f"8명이 되면 입력창·추가 버튼 잠김 (실제 {count}명)")
    ver = await page.evaluate("(document.getElementById('ver') || {}).textContent || ''")
    check(bool(re.match(r"^v\d+\.\d+ · \d{4}-\d{2}-\d{2}$", ver)), f"버전 표시 ({ver})")
    check(not errors, "페이지 오류 없음")
    await ctx.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        await input_rules(browser)
        # 화면 크기·거리·말 수를 섞은 9판을 3판씩 동시에 진행
        for k in range(0, len(CASES), 3):
            await asyncio.gather(*(race(browser, *c) for c in CASES[k:k + 3]))
        await browser.close()
    print()
    if failures:
        print(f"실패 {len(failures)}건")
        sys.exit(1)
    print("모두 통과")


asyncio.run(main())
