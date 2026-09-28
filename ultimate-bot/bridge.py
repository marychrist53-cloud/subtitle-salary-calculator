"""n8n owns the Telegram webhook; this server only accepts forwarded updates."""

import asyncio
import hmac

from aiohttp import web
from telegram import Update


def make_bridge(application, secret):
    async def receive(request):
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, secret):
            raise web.HTTPForbidden()
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("update_id"), int):
                raise ValueError("Invalid update")
            update = Update.de_json(data, application.bot)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text="Invalid Telegram update") from None
        await application.update_queue.put(update)
        return web.Response(text="OK")

    server = web.Application(client_max_size=1024 * 1024)
    server.router.add_post("/" + secret, receive)
    return server


async def run_bridge(application, port, secret, startup):
    async with application:
        runner = web.AppRunner(make_bridge(application, secret), access_log=None)
        await runner.setup()
        try:
            await application.start()
            await web.TCPSite(runner, "0.0.0.0", port).start()
            await startup(application)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            if application.running:
                await application.stop()
