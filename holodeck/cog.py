import asyncio
import datetime
import logging
import os
import shelve
from contextlib import asynccontextmanager

import discord
import racket
import yt_dlp as youtube_dl

from settings import CHANNEL

from .scene import Scene

_log = logging.getLogger(__name__)


SHELVE_LOCATION = "data/scenes.shelve"


async def do_youtube_dl(url: str, loop: asyncio.BaseEventLoop) -> tuple[str, int]:
    """Download a youtube video

    We will return both the path to the downloaded video, as well as
    the duration in seconds.

    Returns:
        A tuple[str, int] of the absoloute path, duration_millis respectively.
    """
    os.makedirs("data/youtubedl", exist_ok=True)

    youtube_dl.utils.bug_reports_message = lambda: ""

    ytdl_format_options = {
        "format": "bestaudio/best",
        # do the conversion using sox
        #'postprocessors': [{
        #'key': 'FFmpegExtractAudio',
        #'preferredcodec': 'mp3',
        #'preferredquality': '192',
        # }],
        "outtmpl": "data/youtubedl/%(extractor)s-%(id)s-%(title)s.%(ext)s",
        "restrictfilenames": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logtostderr": False,
        "quiet": False,
        "no_warnings": False,
        "default_search": "auto",
        "source_address": "0.0.0.0",  # bind to ipv4 since ipv6 addresses cause issues sometimes
    }

    ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

    _log.info("Downloading song from youtube: %s", url)

    def do_download():
        res = ytdl.extract_info(url, download=True)
        return res

    data = ytdl.sanitize_info(await loop.run_in_executor(None, do_download))
    if "entries" in data:
        # take first item from a playlist
        data = data["entries"][0]

    filename = ytdl.prepare_filename(data)
    # labeled_format = filename.rsplit('.', 1)[-1]
    # if labeled_format not in ('mp3', 'webm', 'm4a'):

    return (os.path.relpath(filename), data["duration"] * 1000)


class HolodeckCog(discord.ext.commands.Cog):
    def __init__(self, bot: racket.RacketBot):
        self.bot = bot
        with shelve.open(SHELVE_LOCATION) as s:
            scene_cache = dict(s)
        self._scene_cache = scene_cache
        # TODO: Make a mapping from guild.id to lock.
        self._lock = asyncio.Lock()

    def write_scene(self, scene: Scene):
        with shelve.open(SHELVE_LOCATION) as s:
            s[scene.name] = scene
        self._scene_cache[scene.name] = scene

    @discord.app_commands.command()
    async def add_scene(
        self,
        interaction: discord.Interaction,
        name: str,
        youtube_url: str,
        image_url: str,
        runtime_seconds: discord.app_commands.Range[float, 0.0] = 10.0,
        start_time_seconds: discord.app_commands.Range[float, 0.0] = 0.0,
        overwrite: bool = False,
    ):
        """Create a new scene you can banish people into.

        Args:
            name: The name of your scene.
            youtube_url: The audio for your scene.
            image_url: The image to accompany your scene.
            runtime_seconds: How long the scene should last.
            start_time_seconds: The start time you want for your audio.
            overwrite: Set this true if you're overwriting an existing scene.
        """
        # Note: There's a slight race condition here. Technically some one else
        # can snipe the scene name during yt download below. Not likely, and
        # probably fine if it happens.
        if name in self._scene_cache and not overwrite:
            await interaction.response.send_message(
                f"A scene with the name {name} already exists. "
                "If you want to overwrite it, please provide the "
                "`overwrite=True` option."
            )
            return
        await interaction.response.defer()
        try:
            path, duration_millis = await do_youtube_dl(youtube_url, self.bot.loop)
        except IndexError:
            await interaction.edit_original_response(
                content=f"Failed to download youtube video. Are you sure this is the right URL? `{youtube_url}`"
            )
            return
        runtime_millis = int(runtime_seconds * 1000)
        start_time_millis = int(start_time_seconds * 1000)
        scene = Scene(
            name=name,
            creator=interaction.user.id,
            audio_url=youtube_url,
            audio_path=path,
            start_time_millis=start_time_millis,
            runtime_millis=min(
                30_000, runtime_millis, (duration_millis - start_time_millis)
            ),
            # TODO: We can fetch the image_url from youtube as a default?
            image_url=image_url,
        )
        self.write_scene(scene)
        await interaction.edit_original_response(content=f"Created the scene `{name}`.")

    async def play_file(
        self,
        voice_client: discord.VoiceClient,
        path: str,
        start_millis: int,
        runtime_millis: int,
    ):
        ffmpeg_options = ""
        if start_millis is not None and start_millis > 0:
            ffmpeg_options += " -ss {}".format(
                datetime.timedelta(milliseconds=start_millis)
            )

        if runtime_millis is not None:
            # Note: ffmpeg doesn't do end_time, instead it uses duration, time after start.
            ffmpeg_options += " -t {}".format(
                datetime.timedelta(milliseconds=runtime_millis)
            )

        # Use FFmpegOpusAudio instead of FFmpegPCMAudio
        # This is in case the file we are loading is already opus encoded, preventing double-encoding
        # Consider trying to store audio files in Opus format to decrease load

        # Use before_options to seek to start_time and not read beyond duration
        # if we instead just use options, it will process the whole file but
        # drop the unecessary audio on output
        track = discord.FFmpegOpusAudio(
            path, before_options=ffmpeg_options, options="-filter:a loudnorm"
        )

        if voice_client.is_playing():
            voice_client.stop()

        voice_client.play(track)

    @asynccontextmanager
    async def move_user(self, user: discord.Member, dest: discord.VoiceChannel):
        if user.voice is None or user.voice.channel is None:
            raise ValueError("Cannot move user that is not connected to voice.")
        old_channel = user.voice.channel
        await user.move_to(dest)
        try:
            yield
        finally:
            # If they aren't in voice anymore don't do anything.
            if user.voice is None or user.voice.channel is None:
                pass
            # If they aren't where we moved them, don't do anything.
            elif user.voice.channel.id != dest.id:
                pass
            else:
                await user.move_to(old_channel)

    async def banish_location_autocomplete(
        self, interaction: discord.Interaction, partial_scene: str
    ) -> list[discord.app_commands.Choice[str]]:
        res = [
            discord.app_commands.Choice(name=name, value=name)
            for name in self._scene_cache.keys()
            if (partial_scene.lower() in name.lower())
        ]
        res.sort(key=lambda c: c.name)
        return res

    @discord.app_commands.command()
    @discord.app_commands.autocomplete(where=banish_location_autocomplete)
    async def banish(
        self, interaction: discord.Interaction, who: discord.Member, where: str
    ):
        """Banish someone to a scene of your choosing.

        Args:
            who: The person you would like to banish.
            where: Where you want to send them.
        """
        if where not in self._scene_cache:
            await interaction.response.send_message(
                content=f"I have no record of a scene called {where}.",
                ephemeral=True,
            )
            return
        scene = self._scene_cache[where]

        if who.voice is None or who.voice.channel is None:
            await interaction.response.send_message(
                content=f"User {who.display_name} is not in a voice channel.",
                ephemeral=True,
            )
            return

        # TODO: Add support for custom dest channel, per guild.
        dest_channel = interaction.guild.get_channel_or_thread(CHANNEL)

        if dest_channel is None or not isinstance(dest_channel, discord.VoiceChannel):
            await interaction.response.send_message(
                content=f"Failed to find channel with id: {CHANNEL}",
                ephemeral=True,
            )
            return

        if self._lock.locked():
            await interaction.response.send_message(
                content="The bot is busy at the moment. Please try again later.",
                ephemeral=True,
            )
            return

        async with self._lock:
            voice_client = await dest_channel.connect()
            async with self.move_user(who, dest_channel):
                await self.play_file(
                    voice_client,
                    scene.audio_path,
                    scene.start_time_millis,
                    scene.runtime_millis,
                )
                e = discord.Embed(
                    title="Begone!",
                    description=f"{who.mention} you have been banished to `{scene.name}`",
                )
                e.set_image(url=scene.image_url)
                await interaction.response.send_message(embed=e)
                await asyncio.sleep(scene.runtime_millis / 1000)
            await voice_client.disconnect()
