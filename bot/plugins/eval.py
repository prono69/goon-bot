import io
import json
import sys
import asyncio
import logging
import textwrap
import traceback
from collections import deque
from pyrogram import Client, filters
from pyrogram.types import Message

from bot.config import OWNER_ID  # Using set/list of owner IDs from config

# Constants
COMMAND_TIMEOUT = 120  # Timeout in seconds
MAX_MESSAGE_LENGTH = 4096
COMMAND_ALIASES = {"ll": "ls -l", "sysinfo": "neofetch"}

# Using deque with maxlen handles automatic fixed-length history cleanly
command_history = deque(maxlen=25)

async def aexec(code: str, client: Client, message: Message):
    """Dynamically compiles and runs async Python code with built-in shortcuts."""
    # Define variables inside aexec scope
    p = print
    m = e = event = neo = message
    r = reply = message.reply_to_message
    c = client
    chat = message.chat.id
    to_photo = message.reply_photo
    to_video = message.reply_video

    # Format the code block safely
    code_formatted = textwrap.indent(code, "    ")

    # Pass all shortcut names as parameter names into the generated function
    exec_text = (
        "async def __aexec(client, c, message, m, e, event, neo, r, reply, chat, p, to_photo, to_video):\n"
        f"{code_formatted}"
    )

    exec_globals = {}
    exec_locals = {}

    exec(exec_text, exec_globals, exec_locals)

    # Call the compiled function passing all local variables in matching order
    return await exec_locals["__aexec"](
        client, c, message, m, e, event, neo, r, reply, chat, p, to_photo, to_video
    )


@Client.on_message(filters.command(["eval", "val"]) & filters.user(OWNER_ID))
async def eval_command(client: Client, message: Message):
    """Evaluates arbitrary Python code provided by bot admins."""
    # Prevent IndexError if command has no text argument
    args = message.text.split(" ", maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.reply_text("<b>Error:</b> No Python code provided to execute.")
        return

    cmd = args[1].strip()
    status_message = await message.reply_text("<code>Processing code...</code>")
    reply_to = message.reply_to_message or message

    # Redirect standard streams
    old_stdout, old_stderr = sys.stdout, sys.stderr
    redirected_stdout = sys.stdout = io.StringIO()
    redirected_stderr = sys.stderr = io.StringIO()

    stdout, stderr, exc = None, None, None
    returned_value = None

    try:
        returned_value = await aexec(cmd, client, message)
    except Exception:
        exc = traceback.format_exc().strip()

    stdout = redirected_stdout.getvalue().strip()
    stderr = redirected_stderr.getvalue().strip()

    # Restore standard streams immediately
    sys.stdout = old_stdout
    sys.stderr = old_stderr

    # Determine priority of output result
    output = exc or stderr or stdout or returned_value

    if output is None:
        output = "No output."
    elif isinstance(output, (dict, list)):
        try:
            output = json.dumps(output, indent=4, ensure_ascii=False)
        except Exception:
            output = str(output)
    else:
        output = str(output)

    # Clean raw output string (for file upload)
    raw_response = f"COMMAND:\n{cmd}\n\nOUTPUT:\n{output}"

    # Formatting HTML message for chat
    formatted_html = (
        f"<b>EVAL CODE:</b>\n<code>{cmd}</code>\n\n"
        f"<b>OUTPUT:</b>\n<code>{output}</code>"
    )

    # Pyrogram max message length limit handler (4096 characters)
    if len(formatted_html) > 4096:
        with io.BytesIO(raw_response.encode("utf-8")) as out_file:
            out_file.name = "eval_output.txt"
            await reply_to.reply_document(
                document=out_file,
                caption=f"<code>{cmd[:100]}...</code>",
                quote=True,
            )
    else:
        await reply_to.reply_text(formatted_html, quote=True)

    await status_message.delete()


@Client.on_message(filters.command(["bash", "sh"]) & filters.user(OWNER_ID))
async def execution(_, message: Message):
    # Prevent IndexError if no command argument is passed
    args = message.text.split(" ", maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.reply_text("<b>Error:</b> No shell command provided.")
        return

    cmd = args[1].strip()
    cmd = COMMAND_ALIASES.get(cmd, cmd)
    reply_to = message.reply_to_message or message

    status_message = await message.reply_text("<code>Processing shell command...</code>")

    try:
        logging.info(f"Shell execution by {message.from_user.id}: {cmd}")

        # Spawn asynchronous subprocess
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=COMMAND_TIMEOUT
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await status_message.edit_text("❌ <b>Timeout:</b> Process exceeded time limit.")
            return

        stderr = stderr_bytes.decode("utf-8", errors="replace").strip() or "None"
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip() or "None"

        # HTML response format for Telegram chat view
        formatted_html = (
            f"<b>QUERY:</b> <code>{cmd}</code>\n"
            f"<b>PID:</b> <code>{process.pid}</code>\n\n"
            f"<b>STDERR:</b>\n<code>{stderr}</code>\n\n"
            f"<b>STDOUT:</b>\n<code>{stdout}</code>"
        )

        # Pyrogram 4096 character message limit handler
        if len(formatted_html) > MAX_MESSAGE_LENGTH:
            # Clean raw output format without HTML formatting for document attachment
            raw_text = (
                f"COMMAND:\n{cmd}\nPID: {process.pid}\n\n"
                f"STDERR:\n{stderr}\n\n"
                f"STDOUT:\n{stdout}"
            )
            with io.BytesIO(raw_text.encode("utf-8")) as out_file:
                out_file.name = f"shell_{process.pid}.txt"
                await reply_to.reply_document(
                    document=out_file,
                    caption=f"<code>{cmd[:100]}...</code>",
                    disable_notification=True,
                    quote=True,
                )
        else:
            await reply_to.reply_text(formatted_html, quote=True)

        # Track history using deque
        command_history.append(cmd)

    except Exception as ex:
        await reply_to.reply_text(f"❌ <b>Error:</b> <code>{ex}</code>", quote=True)
    finally:
        await status_message.delete()
