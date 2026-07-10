"""Example Kitty hook that logs completed turns."""

listened_events = ["cli.turn_done"]


async def hook(event, ctx):
    ctx.logger.info(
        "echo_hook turn completed session=%s request=%s",
        event.session_id,
        event.data.get("request_id", ""),
    )


hook.listened_events = listened_events
