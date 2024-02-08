import racket

import holodeck
from settings import BOT_TOKEN, GUILD_IDS


def main():
    racket.run_cog(holodeck.HolodeckCog, guilds=GUILD_IDS, token=BOT_TOKEN)


if __name__ == "__main__":
    main()
