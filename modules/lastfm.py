from __future__ import annotations

import asyncio
import datetime
import pprint
import time
import traceback
from typing import TYPE_CHECKING, Optional

import disnake
from disnake.ext import commands

from utils.db import DBModel
from utils.music.lastfm_tools import lastfm_get_session_key, lastfm_get_token, generate_api_sig, LastFmException, post_lastfm, \
    lastfm_update_nowplaying, lastfm_track_scrobble, lastfm_user_info
from utils.music.models import LavalinkPlayer, LavalinkTrack
from utils.others import CustomContext

if TYPE_CHECKING:
    from utils.client import BotCore


class LastFMView(disnake.ui.View):

    def __init__(self, ctx, session_key: str):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.interaction: Optional[disnake.MessageInteraction] = None
        self.session_key = ""
        self.username = ""
        self.token = ""
        self.last_timestamp = None
        self.auth_url = None
        self.skg = None
        self.network = None
        self.clear_session = False
        self.check_loop = None
        self.error = None
        self.cooldown = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.user)


        if session_key:
            btn2 = disnake.ui.Button(label="Unlink last.fm account", style=disnake.ButtonStyle.red)
            btn2.callback = self.disconnect_account
            self.add_item(btn2)

        else:
            btn = disnake.ui.Button(label="Link last.fm account")
            btn.callback = self.send_authurl_callback
            self.add_item(btn)

    async def check_session_loop(self):

        count = 15

        while count > 0:
            try:
                await asyncio.sleep(20)
                data = await lastfm_get_session_key(
                    api_key=self.ctx.bot.config["LASTFM_KEY"],
                    api_secret=self.ctx.bot.config["LASTFM_SECRET"],
                    token=self.token
                )
                if data.get('error'):
                    count -= 1
                    continue
                self.session_key = data["session"]["key"]
                self.username = data["session"]["name"]
                self.stop()
                return
            except Exception as e:
                self.error = e
                self.auth_url = ""
                self.token = ""
                self.stop()
                return

        self.auth_url = ""
        self.token = ""

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.send("You can't use this button", ephemeral=True)
            return False
        return True

    async def disconnect_account(self, interaction: disnake.MessageInteraction):
        self.clear_session = True
        self.session_key = ""
        self.interaction = interaction
        self.stop()

    async def send_authurl_callback(self, interaction: disnake.MessageInteraction):

        self.check_loop = self.ctx.bot.loop.create_task(self.check_session_loop())

        if not self.auth_url:
            self.token = await lastfm_get_token(apikey=self.ctx.bot.config["LASTFM_KEY"])
            self.auth_url = f'http://www.last.fm/api/auth/?api_key={self.ctx.bot.config["LASTFM_KEY"]}&token={self.token}'
            self.last_timestamp = int((disnake.utils.utcnow() + datetime.timedelta(minutes=5)).timestamp())

        await interaction.send(f"### [Click here](<{self.auth_url}>) to link your last.fm account (on the page click \"allow\")\n\n"
                               f"`The link expires in` <t:{self.last_timestamp}:R> `(If it is expired, click the button again).`\n\n"
                               f"`Attention: Do not show the \"click here\" link to anyone or send it in public places, "
                               f"as this link can grant access to your last.fm account`\n\n"
                               "`If you have already authorized the application you must wait up to 20 seconds for the "
                               "above message to update confirming the process.`",
                               ephemeral=True, delete_after=300)

class LastFmCog(commands.Cog):

    emoji = "🎧"
    name = "LastFM"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot

        if not hasattr(bot.pool, "lastfm_sessions"):
            bot.pool.lastfm_sessions = {}

    lastfm_cd = commands.CooldownMapping.from_cooldown(1, 30, commands.BucketType.member)
    lastfm_mc = commands.MaxConcurrency(1, per=commands.BucketType.user, wait=False)

    @commands.command(hidden=True, name="lastfm", aliases=["lastfmconnect", "lfm"],
                      description="Connect your last.fm account.",
                      cooldown=lastfm_cd, max_concurrency=lastfm_mc)
    async def lastfmconnect_legacy(self, ctx: CustomContext):
        await self.lastfm.callback(self=self, inter=ctx)


    @commands.slash_command(hidden=True, name="lastfm",
                      description=f"{desc_prefix}Connect your last.fm account",
                      cooldown=lastfm_cd, max_concurrency=lastfm_mc)
    async def lastfm(self, inter: disnake.AppCmdInter):

        cog = self.bot.get_cog("Music")

        if cog:
            await inter.response.defer(ephemeral=await cog.is_request_channel(inter, ignore_thread=True))
        else:
            await inter.response.defer(ephemeral=True)

        try:
            data = inter.global_user_data
        except AttributeError:
            data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)
            try:
                inter.global_user_data = data
            except:
                pass

        lastfm_user = None

        if current_session_key:=data["lastfm"]["sessionkey"]:
            try:
                lastfm_user = await lastfm_user_info(current_session_key, self.bot.config["LASTFM_KEY"])
            except LastFmException as e:
                if e.code == 9:
                    data["lastfm"]["sessionkey"] = ""
                    data["lastfm"]["username"] = ""
                    current_session_key = ""
                    await self.bot.update_global_data(inter.author.id, data, db_name=DBModel.users)
                else:
                    raise e

        else:
            data["lastfm"]["sessionkey"] = ""
            data["lastfm"]["username"] = ""
            current_session_key = ""
            await self.bot.update_global_data(inter.author.id, data, db_name=DBModel.users)

        if lastfm_user:
            txt = f"👤 **⠂User:** [`{lastfm_user['realname']}`](<{lastfm_user['url']}>)\n\n" \
                  f"⏰ **⠂Account created on:** <t:{lastfm_user['registered']['#text']}:f>\n\n" \
                  f"🌎 **⠂Country:** `{lastfm_user['country']}`\n\n"

            if playcount := lastfm_user['playcount']:
                txt += f"▶️ **⠂Tracks played:** [`{playcount}`](<https://www.last.fm/user/{lastfm_user['name']}/library>)\n\n"

            if playlists := lastfm_user['playlists'] != "0":
                txt += f"📄 **⠂Playlists:** [`{playlists}`](<https://www.last.fm/user/{lastfm_user['name']}/playlists>)\n\n"

            if playcount := lastfm_user['track_count']:
                txt += f"🎵 **⠂Tracks scrobbled:** [`{playcount}`](<https://www.last.fm/user/{lastfm_user['name']}/library/tracks>)\n\n"

            if artists := lastfm_user['artist_count']:
                txt += f"🎧 **⠂Artists scrobbled:** [`{artists}`](<https://www.last.fm/user/{lastfm_user['name']}/library/artists>)\n\n"

            if albums := lastfm_user['album_count']:
                txt += f"📀 **⠂Albums scrobbled:** [`{albums}`](<https://www.last.fm/user/{lastfm_user['name']}/library/albums>)\n\n"

            embed = disnake.Embed(
                description=txt, color=self.bot.get_color()
            ).set_thumbnail(url=lastfm_user['image'][-1]["#text"]).set_author(
                name="Last.FM: User Information",
                icon_url="https://www.last.fm/static/images/lastfm_avatar_twitter.52a5d69a85ac.png")

        else:
            embed = disnake.Embed(
                description="**Link (or create) an account on [last.fm](<https://www.last.fm/home>) to register "
                            "all the tracks you listen to here on your last.fm profile to get music/artist/album suggestions "
                            "and have an overall statistic of the tracks you have listened to as well as access to an amazing community on the platform.**",
                color=self.bot.get_color()
            ).set_thumbnail(url="https://www.last.fm/static/images/lastfm_avatar_twitter.52a5d69a85ac.png")

        view = LastFMView(inter, session_key=current_session_key)

        if isinstance(inter, CustomContext):
            msg = await inter.send(embed=embed, view=view)
            inter.store_message = msg
        else:
            msg = None
            await inter.edit_original_message(embed=embed, view=view)

        await view.wait()

        for c in view.children:
            c.disabled = True

        if not view.session_key and not view.clear_session:

            if view.error:
                raise view.error

            embed.set_footer(text="The time to link your last.fm account has expired! Use the command again if you want to repeat the process.")

            if msg:
                await msg.edit(embed=embed, view=view)
            else:
                await inter.edit_original_message(embed=embed, view=view)

            return

        newdata = {"scrobble": True, "sessionkey": view.session_key, "username": view.username}
        data["lastfm"].update(newdata)
        await self.bot.update_global_data(inter.author.id, data=data, db_name=DBModel.users)

        self.bot.pool.lastfm_sessions[inter.author.id] = newdata

        embed.clear_fields()

        if view.session_key:
            embed.description += f"\n### The account [{view.username}](<https://www.last.fm/user/{view.username}>) was " + \
                                 "successfully linked!\n\n`Now when you listen to your tracks in the voice channel they will be registered " \
                                "on your last.fm account`"
        else:
            embed.description += "\n### Account successfully unlinked!"

        if view.interaction:
            await view.interaction.response.edit_message(embed=embed, view=view, content=None)
        elif msg:
            await msg.edit(embed=embed, view=view, content=None)
        else:
            await inter.edit_original_message(embed=embed, view=view, content=None)

    @commands.Cog.listener("on_voice_state_update")
    async def connect_vc_update(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):

        if member.bot or not after.channel or before.channel == after.channel:
            return

        try:
            player: LavalinkPlayer = self.bot.music.players[member.guild.id]
        except KeyError:
            return

        if player.last_channel != after.channel:
            return

        try:
            if not player.current or member not in player.last_channel.members:
                return
        except AttributeError:
            return

        try:
            fm_user = player.lastfm_users[member.id]
        except KeyError:
            pass
        else:
            if fm_user["last_url"] == player.current.uri and fm_user["last_timestamp"] and datetime.datetime.utcnow() < fm_user["last_timestamp"]:
                return

        await self.startscrooble(player=player, track=player.last_track, users=[member])

    @commands.Cog.listener('on_wavelink_track_start')
    async def update_np(self, player: LavalinkPlayer):
        await self.startscrooble(player, track=player.current, update_np=True)

    @commands.Cog.listener('on_wavelink_track_end')
    async def startscrooble(self, player: LavalinkPlayer, track: LavalinkTrack, reason: str = None, update_np=False, users=None):

        if not track or track.is_stream or track.info["sourceName"] in ("local", "http"):
            return

        if not update_np:

            if reason != "FINISHED":
                return

            if track.duration < 20000:
                return

            if (disnake.utils.utcnow() - player.start_time).total_seconds() < ((track.duration * 0.75) / 1000):
                return

        counter = 3

        while counter > 0:
            if not player.guild.me.voice:
                await asyncio.sleep(2)
                continue
            break

        if not player.guild.me.voice:
            return

        if track.info["sourceName"] in ("youtube", "soundcloud"):

            if track.ytid:
                if track.author.endswith(" - topic") and not track.author.endswith("Release - topic") and not track.title.startswith(track.author[:-8]):
                    name = track.title
                    artist = track.author[:-8]
                else:
                    try:
                        artist, name = track.title.split(" - ", maxsplit=1)
                    except ValueError:
                        name = track.title
                        artist = track.author
            else:
                name = track.single_title
                artist = track.author

            artist = artist.split(",")[0]

        else:
            artist = track.author.split(",")[0]
            name = track.single_title

        duration = int(track.duration / 1000)

        if not (album:=track.album_name) and not track.autoplay and track.info["sourceName"] in ("spotify", "deezer", "applemusic", "tidal"):
            album = track.single_title

        for user in users or player.last_channel.members:

            if user.bot:
                continue

            try:
                if user.voice.self_deaf or user.voice.deaf:
                    continue
            except AttributeError:
                continue

            try:
                fminfo = self.bot.pool.lastfm_sessions[user.id]
            except KeyError:
                user_data = await self.bot.get_global_data(user.id, db_name=DBModel.users)
                fminfo = user_data["lastfm"]
                self.bot.pool.lastfm_sessions[user.id] = fminfo

            if fminfo["scrobble"] is False or not fminfo["sessionkey"]:
                continue

            try:
                kwargs = {
                    "artist": artist, "track": name, "album": album, "duration": duration,
                    "session_key": fminfo["sessionkey"], "api_key": self.bot.config["LASTFM_KEY"],
                    "api_secret": self.bot.config["LASTFM_SECRET"]
                }
                if update_np:
                    await lastfm_update_nowplaying(**kwargs)
                else:
                    await lastfm_track_scrobble(**kwargs)
            except Exception as e:
                if isinstance(e, LastFmException):
                    print(f"last.fm failed! user: {user.id} - code: {e.code} - message: {e.message}")
                    if e.code == 9:
                        user_data = await self.bot.get_global_data(user.id, db_name=DBModel.users)
                        user_data["lastfm"]["sessionkey"] = ""
                        await self.bot.update_global_data(user.id, user_data, db_name=DBModel.users)
                        try:
                            del self.bot.pool.lastfm_sessions[user.id]
                        except KeyError:
                            pass
                        try:
                            del player.lastfm_users[user.id]
                        except KeyError:
                            pass
                        continue
                traceback.print_exc()
                continue

            player.lastfm_users[user.id] = {
                "last_url": track.url,
                "last_timestamp": datetime.datetime.utcnow() + datetime.timedelta(seconds=duration)
            }


def setup(bot):
    if not bot.pool.config["LASTFM_KEY"] or not bot.pool.config["LASTFM_SECRET"]:
        print(("="*48) + "\n⚠️ - Last.FM features will be disabled due to lack of LASTFM_KEY and LASTFM_SECRET configuration")
        return
    bot.add_cog(LastFmCog(bot))
