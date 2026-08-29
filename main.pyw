import logging
import logging_setup

# error logging (installed before any other import, as upstream did, so that
# import-time failures still reach DAP_errors.log)
logging_setup.configure()

import sys
import cli
import sound
import instrumentation
import asyncio
import discord
import argparse

# commandline args
parser = argparse.ArgumentParser(description="Discord Audio Pipe")
connect = parser.add_argument_group("Command Line Mode")
query = parser.add_argument_group("Queries")

parser.add_argument(
    "-t",
    "--token",
    dest="token",
    action="store",
    default=None,
    help="The token for the bot",
)

parser.add_argument(
    "-v",
    "--verbose",
    dest="verbose",
    action="store_true",
    help="Enable verbose logging",
)

parser.add_argument(
    "--diagnose",
    dest="diagnose",
    action="store_true",
    help="Enable audio/voice-state diagnostics (see DAP_session.log)",
)

connect.add_argument(
    "-c",
    "--channel",
    dest="channel",
    action="store",
    type=int,
    help="The channel to connect to as an id",
)

connect.add_argument(
    "-d",
    "--device",
    dest="device",
    action="store",
    type=int,
    help="The device to listen from as an index",
)

query.add_argument(
    "-D",
    "--devices",
    dest="query",
    action="store_true",
    help="Query compatible audio devices",
)

query.add_argument(
    "-C",
    "--channels",
    dest="online",
    action="store_true",
    help="Query servers and channels (requires token)",
)

args = parser.parse_args()
is_gui = not any([args.channel, args.device, args.query, args.online])

# verbose logs
if args.verbose:
    logging_setup.enable_verbose()

# diagnostics
if args.diagnose:
    instrumentation.enable()

logging_setup.log_start(args, diagnose=args.diagnose)

# don't import qt stuff if not using gui
if is_gui:
    import gui
    from PyQt6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Information)

# main
async def main(bot):
    try:
        # query devices
        if args.query:
            for device, index in sound.query_devices().items():
                print(index, device)

            return

        # check for token
        token = args.token
        if token is None:
            token = open("token.txt", "r").read()

        # query servers and channels
        if args.online:
            await cli.query(bot, token)

            return

        # GUI
        if is_gui:
            bot_ui = gui.GUI(app, bot)
            asyncio.create_task(bot_ui.ready())
            asyncio.create_task(bot_ui.run_Qt())

        # CLI
        else:
            asyncio.create_task(cli.connect(bot, args.device, args.channel))

        await bot.start(token)

    except FileNotFoundError:
        if is_gui:
            msg.setWindowTitle("Token Error")
            msg.setText("No Token Provided")
            msg.exec()

        else:
            print("No Token Provided")

    except discord.errors.LoginFailure:
        if is_gui:
            msg.setWindowTitle("Login Failed")
            msg.setText("Please check if the token is correct")
            msg.exec()

        else:
            print("Login Failed: Please check if the token is correct")

    except asyncio.CancelledError:
        if is_gui:
            bot_ui.close()

        await bot.close()
        await asyncio.sleep(1)
        raise

    except Exception:
        logging.exception("Error on main")

bot = discord.Client(intents=discord.Intents.default())

try:
    asyncio.run(main(bot))
except KeyboardInterrupt:
    print("Exiting...")
finally:
    logging_setup.log_shutdown()