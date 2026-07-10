events = []
listened_events = ["cli.wire", "cli.turn_done"]


async def hook(event, ctx):
    events.append(
        {
            "event_type": event.event_type,
            "session_id": event.session_id,
            "title": ctx.record.meta.title if ctx.record.meta else "",
            "work_dir": str(ctx.work_dir),
        }
    )


hook.listened_events = listened_events
