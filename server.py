async def fetch_admiral():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="de-AT"
        )
        page = await context.new_page()

        url = (
            "https://widget-api.admiral.at/api/cms/views/dynamic"
            "?alternativeIds=asw:category:10000002"
            "&viewId=sportsBetting"
            "&viewClass=asw-sports-betting"
        )

        response = await page.goto(url)
        data = await response.json()

        await browser.close()
        return data
