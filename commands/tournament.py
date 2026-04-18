import uuid
import os
import time
import discord
from discord import app_commands
from core.bot_instance import bot
from core.config import SERVER_ID, ROLE_ID, RAILWAY_BASE, is_authorized, guild_object
from core.storage import tournaments, save_tournaments, save_config, active_matches, _dashboard_tokens
from views.registration import SignupView
from views.vod import VODSubmissionView
from services.challonge import challonge_get_participants


# ── /tournament-setup ──────────────────────────────────────────────────────────
class TournamentCategorySelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="Tournament channels (signups, updates, matches, pick/ban)...",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.tournament_category_id = int(self.values[0])
        await interaction.response.defer()


class AdminCategorySelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="Admin channels (tournament-admin)...",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.admin_category_id = int(self.values[0])
        await interaction.response.defer()


class SetupView(discord.ui.View):
    def __init__(self, options):
        super().__init__(timeout=120)
        self.tournament_category_id = None
        self.admin_category_id = None
        self.add_item(TournamentCategorySelect(options))
        self.add_item(AdminCategorySelect(options))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.tournament_category_id or not self.admin_category_id:
            await interaction.response.send_message("Please select both categories before saving.", ephemeral=True)
            return

        save_config({
            "tournament_category_id": self.tournament_category_id,
            "admin_category_id": self.admin_category_id,
        })

        t_cat = interaction.guild.get_channel(self.tournament_category_id)
        a_cat = interaction.guild.get_channel(self.admin_category_id)

        await interaction.response.send_message(
            f"Setup saved!\n"
            f"Tournament channels -> **{t_cat.name if t_cat else 'Unknown'}**\n"
            f"Admin channels -> **{a_cat.name if a_cat else 'Unknown'}**",
            ephemeral=True,
        )


@bot.tree.command(
    name="tournament-setup",
    description="Configure which categories tournament channels are placed in.",
    guild=guild_object(),
)
@is_authorized()
async def tournament_setup(interaction: discord.Interaction):
    categories = interaction.guild.categories
    if not categories:
        await interaction.response.send_message("No categories found in this server.", ephemeral=True)
        return

    options = [
        discord.SelectOption(label=cat.name[:100], value=str(cat.id))
        for cat in categories[:25]
    ]

    view = SetupView(options=options)
    await interaction.response.send_message(
        "**Tournament Setup**\nSelect which category each group of channels should go in:",
        view=view,
        ephemeral=True,
    )


# ── /tournament-create ──────────────────────────────────────────────────────────
class TournamentCreateModal(discord.ui.Modal, title="Create Tournament"):
    tournament_name = discord.ui.TextInput(
        label="Tournament Name",
        placeholder="e.g. FRVR X NACK $700 4v4 Tournament",
        max_length=100,
    )
    format = discord.ui.TextInput(
        label="Format",
        placeholder="1v1 | 2v2 | 3v3 | 4v4",
        max_length=10,
    )
    prize_pool = discord.ui.TextInput(
        label="Prize Pool (1st / 2nd / 3rd)",
        placeholder="e.g. 420 / 210 / 70",
        max_length=50,
    )
    date = discord.ui.TextInput(
        label="Date (Discord Timestamp)",
        placeholder="e.g. <t:1743188400:F>  -- use discordtimestamp.com",
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction):
        from utils.helpers import get_categories

        guild = interaction.guild
        fmt = self.format.value.strip().lower()
        valid_formats = {"1v1": 1, "2v2": 2, "3v3": 3, "4v4": 4}
        if fmt not in valid_formats:
            await interaction.response.send_message(
                "Invalid format. Must be one of: `1v1`, `2v2`, `3v3`, `4v4`.",
                ephemeral=True,
            )
            return

        team_size = valid_formats[fmt]
        tournament_id = str(uuid.uuid4())[:8].upper()
        raw_prizes = self.prize_pool.value.strip()
        prize_parts = [p.strip().lstrip("$") for p in raw_prizes.split("/")]
        prize_display = " / ".join(f"${p}" for p in prize_parts)

        t_cat, _ = get_categories(guild)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, view_channel=True),
            guild.me: discord.PermissionOverwrite(send_messages=True, manage_channels=True),
        }
        signups_channel = await guild.create_text_channel(
            name="signups",
            overwrites=overwrites,
            topic=f"Tournament registration -- ID: {tournament_id}",
            category=t_cat,
        )

        embed = discord.Embed(
            title=f"{self.tournament_name.value}  --  Registration",
            description=f"Click the button below to register for the **{self.tournament_name.value}**!",
            color=0x00FF7F,
        )
        embed.add_field(name="Prizes", value=prize_display, inline=True)
        embed.add_field(name="Format", value=self.format.value.upper(), inline=True)
        embed.add_field(name="Date", value=self.date.value, inline=True)
        embed.set_footer(text=f"Tournament ID: {tournament_id}")

        tournaments[tournament_id] = {
            "id": tournament_id,
            "name": self.tournament_name.value,
            "format": fmt,
            "team_size": team_size,
            "prizes": prize_display,
            "date": self.date.value,
            "signups_channel_id": signups_channel.id,
            "organizer_id": interaction.user.id,
            "organizer_name": interaction.user.display_name,
            "teams": [],
            "open": True,
        }

        view = SignupView(tournament_id=tournament_id)
        save_tournaments()
        signup_btn_msg = await signups_channel.send(embed=embed, view=view)
        tournaments[tournament_id]["signup_button_message_id"] = signup_btn_msg.id
        save_tournaments()
        await interaction.response.send_message(
            f"Tournament **{self.tournament_name.value}** created!\n"
            f"Sign-ups are open in {signups_channel.mention}\n"
            f"Tournament ID: `{tournament_id}`",
            ephemeral=True,
        )


@bot.tree.command(name="tournament-create", description="Create a new tournament and open sign-ups.", guild=guild_object())
@is_authorized()
async def tournament_create(interaction: discord.Interaction):
    await interaction.response.send_modal(TournamentCreateModal())


# ── /tournament-start ──────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-start", description="Close sign-ups and start the tournament.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID from /tournament-create")
async def tournament_start(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can start this tournament.", ephemeral=True)
        return

    if not t["open"]:
        await interaction.response.send_message("This tournament has already started.", ephemeral=True)
        return

    if not t.get("admin_token"):
        t["admin_token"] = str(uuid.uuid4())
        save_tournaments()

    seeding_url = f"{RAILWAY_BASE}/admin/{t_id}/seeding?token={t['admin_token']}"
    await interaction.response.send_message(
        f"**Open the seeding page to reorder teams and start the bracket:**\n{seeding_url}",
        ephemeral=True,
    )


# ── /tournament-info ──────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-info", description="Open the tournament dashboard.", guild=guild_object())
@is_authorized()
async def tournament_info(interaction: discord.Interaction):
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if not dashboard_token:
        dashboard_token = str(uuid.uuid4())
        _dashboard_tokens.add(dashboard_token)

    url = f"{RAILWAY_BASE}/dashboard?token={dashboard_token}"
    await interaction.response.send_message(f"**Tournament Dashboard:**\n{url}", ephemeral=True)


# ── /tournament-edit ──────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-edit", description="Edit tournament name, prize pool, or date.", guild=guild_object())
@is_authorized()
@app_commands.describe(
    tournament_id="The tournament ID",
    name="New tournament name (leave blank to keep current)",
    prizes="New prize pool, e.g. '420 / 210 / 70' (leave blank to keep current)",
    date="New date, Discord timestamp format e.g. <t:1743188400:F> (leave blank to keep current)",
)
async def tournament_edit(
    interaction: discord.Interaction,
    tournament_id: str,
    name: str | None = None,
    prizes: str | None = None,
    date: str | None = None,
):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can edit this tournament.", ephemeral=True)
        return

    if not any([name, prizes, date]):
        await interaction.response.send_message(
            "Nothing to update — provide at least one of `name`, `prizes`, or `date`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    changes = []

    if name:
        old = t.get("name")
        new = name.strip()
        t["name"] = new
        changes.append(f"Name: **{old}** → **{new}**")

    if prizes:
        raw = prizes.strip()
        prize_parts = [p.strip().lstrip("$") for p in raw.split("/")]
        prize_display = " / ".join(f"${p}" for p in prize_parts)
        old = t.get("prizes", "N/A")
        t["prizes"] = prize_display
        changes.append(f"Prizes: **{old}** → **{prize_display}**")

    if date:
        old = t.get("date", "N/A")
        t["date"] = date.strip()
        changes.append(f"Date: **{old}** → **{date.strip()}**")

    save_tournaments()

    # If signup button is still live, update its embed to reflect changes
    from views.registration import SignupView
    signups_ch_id = t.get("signups_channel_id")
    btn_msg_id = t.get("signup_button_message_id")
    if signups_ch_id and btn_msg_id:
        signups_ch = interaction.guild.get_channel(signups_ch_id)
        if signups_ch:
            try:
                btn_msg = await signups_ch.fetch_message(btn_msg_id)
                new_embed = discord.Embed(
                    title=f"{t['name']}  --  Registration",
                    description=f"Click the button below to register for the **{t['name']}**!",
                    color=0x00FF7F,
                )
                new_embed.add_field(name="Prizes", value=t.get("prizes", "N/A"), inline=True)
                new_embed.add_field(name="Format", value=t.get("format", "N/A").upper(), inline=True)
                new_embed.add_field(name="Date", value=t.get("date", "N/A"), inline=True)
                new_embed.set_footer(text=f"Tournament ID: {t_id}")
                await btn_msg.edit(embed=new_embed, view=SignupView(tournament_id=t_id))
            except Exception as e:
                print(f"[Edit] Failed to update signup button embed: {e}")

    await interaction.followup.send(
        f"**Tournament `{t_id}` updated:**\n" + "\n".join(changes),
        ephemeral=True,
    )


# ── /tournament-remove-team ───────────────────────────────────────────────────
@bot.tree.command(name="tournament-remove-team", description="Remove a team from a tournament.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID", team_name="The team name to remove")
async def tournament_remove_team(interaction: discord.Interaction, tournament_id: str, team_name: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    found_team = None
    for tm in t.get("teams", []):
        if tm["team_name"].lower() == team_name.lower():
            found_team = tm
            break

    if not found_team:
        team_list = ", ".join(tm["team_name"] for tm in t.get("teams", []))
        await interaction.response.send_message(
            f"Team `{team_name}` not found.\nRegistered teams: {team_list or 'None'}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    signups_ch = interaction.guild.get_channel(t.get("signups_channel_id"))
    if signups_ch and found_team.get("signup_message_id"):
        try:
            msg = await signups_ch.fetch_message(found_team["signup_message_id"])
            await msg.delete()
        except Exception:
            pass

    t["teams"].remove(found_team)
    save_tournaments()

    await interaction.followup.send(f"Team **{found_team['team_name']}** has been removed from `{t_id}`.")


# ── /tournament-end ───────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-end", description="End a tournament -- clean up channels/roles but keep results.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID to end")
async def tournament_end(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can end this tournament.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # ── Determine top 2 teams from Challonge standings ──────────────────────
    vod_team_ids = set()  # team_ids that need VOD submissions (keep their channels)
    challonge_id = t.get("challonge_id")
    participant_map = t.get("challonge_participant_map", {})
    top_2_teams = []  # list of (team_dict, placement_str)

    if challonge_id:
        try:
            participants = await challonge_get_participants(challonge_id)
            print(f"[VOD] Fetched {len(participants)} participants from Challonge")

            # Sort participants by final_rank (1st, 2nd)
            ranked = sorted(
                [p for p in participants if p.get("final_rank")],
                key=lambda p: p["final_rank"],
            )
            print(f"[VOD] Participants with final_rank: {len(ranked)}")

            if ranked:
                # Challonge has final standings
                placement_labels = {1: "1st Place", 2: "2nd Place"}
                for p in ranked[:2]:
                    rank = p["final_rank"]
                    pname = p.get("name", "")
                    # Find our team by name (most reliable)
                    team = next((tm for tm in t["teams"] if tm["team_name"] == pname), None)
                    if not team:
                        # Try via participant_map
                        c_pid = p["id"]
                        for tname, mapped_pid in participant_map.items():
                            if mapped_pid == c_pid:
                                team = next((tm for tm in t["teams"] if tm["team_name"] == tname), None)
                                break
                    if team:
                        top_2_teams.append((team, placement_labels.get(rank, f"#{rank}")))
            else:
                # No final_rank -- tournament may not be finalized on Challonge
                # Fallback: get all completed matches, find the grand finals participants
                from services.challonge import challonge_get_matches
                all_matches = await challonge_get_matches(challonge_id, state="all")
                print(f"[VOD] Fallback: checking {len(all_matches)} matches for finalists")

                # Find the match with the highest round number (grand finals)
                if all_matches:
                    grand_final = max(all_matches, key=lambda m: m.get("round", 0))
                    gf_p1 = grand_final.get("player1_id")
                    gf_p2 = grand_final.get("player2_id")
                    winner_id = grand_final.get("winner_id")

                    if winner_id:
                        # Tournament has a winner
                        order = [(winner_id, "1st Place"),
                                 (gf_p1 if gf_p1 != winner_id else gf_p2, "2nd Place")]
                    else:
                        # No winner yet -- just grab both finalists
                        order = [(gf_p1, "Finalist"), (gf_p2, "Finalist")]

                    for c_pid, label in order:
                        if not c_pid:
                            continue
                        # Find participant name
                        p = next((pp for pp in participants if pp["id"] == c_pid), None)
                        if not p:
                            continue
                        pname = p.get("name", "")
                        team = next((tm for tm in t["teams"] if tm["team_name"] == pname), None)
                        if team:
                            top_2_teams.append((team, label))
        except Exception as e:
            print(f"[VOD] Failed to fetch Challonge standings: {e}")

    # If Challonge didn't give us results, skip VOD embeds
    if not top_2_teams:
        print(f"[VOD] No top 2 teams found -- skipping VOD embeds")

    deadline_ts = int(time.time()) + (7 * 24 * 60 * 60)  # 1 week from now

    for team, placement in top_2_teams:
        our_team_id = team["team_id"]
        vod_team_ids.add(our_team_id)

        # Send VOD embed to team's text channel
        ch_ids = t.get("team_channels", {}).get(our_team_id, {})
        text_ch_id = ch_ids.get("text")
        if not text_ch_id:
            print(f"[VOD] No text channel found for {team['team_name']}")
            continue

        text_ch = guild.get_channel(text_ch_id)
        if not text_ch:
            print(f"[VOD] Text channel {text_ch_id} not found in guild for {team['team_name']}")
            continue

        embed = discord.Embed(
            title=f"VOD Submission Required: {team['team_name']} ({placement})",
            description=(
                f"Congratulations! All players in this team must submit their POV VOD recordings for review.\n\n"
                f"**Deadline:** <t:{deadline_ts}:F> (<t:{deadline_ts}:R>)\n\n"
                f"**Only click your own button.**"
            ),
            color=0xFFAA00,
        )
        embed.set_footer(text=f"Tournament: {t['name']}")

        players = team.get("players", [])
        view = VODSubmissionView(
            tournament_id=t_id,
            team_id=our_team_id,
            players=players,
        )

        # Ping the team role
        role_id = t.get("team_roles", {}).get(our_team_id)
        role = guild.get_role(role_id) if role_id else None
        ping = role.mention if role else " ".join(f"<@{p['discord_id']}>" for p in players)

        await text_ch.send(
            ping,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        print(f"[VOD] Sent VOD submission embed to {team['team_name']} ({placement})")

        # Store VOD info on tournament for persistence
        if "vod_teams" not in t:
            t["vod_teams"] = {}
        t["vod_teams"][our_team_id] = {
            "team_name": team["team_name"],
            "placement": placement,
            "deadline": deadline_ts,
            "channel_id": text_ch_id,
        }

    # Delete match room channels from active_matches
    match_ids_to_remove = []
    for mid, match in active_matches.items():
        if match.get("tournament_id") == t_id:
            ch_id = match.get("channel_id")
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
            match_ids_to_remove.append(mid)
    for mid in match_ids_to_remove:
        del active_matches[mid]

    # Also delete any channels in the Matchrooms category
    matchrooms_cat = discord.utils.get(guild.categories, name="Matchrooms")
    if matchrooms_cat:
        for ch in matchrooms_cat.channels:
            try:
                await ch.delete()
            except Exception:
                pass
        try:
            await matchrooms_cat.delete()
        except Exception:
            pass

    # Delete tournament channels (but archive tournament-updates for history)
    for key in ["signups_channel_id", "admin_channel_id", "caster_channel_id"]:
        ch_id = t.get(key)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass

    # Archive the tournament-updates channel: rename + make read-only + move out of
    # the tournament category so it survives cleanup. Scoreboard images remain accessible.
    updates_ch_id = t.get("updates_channel_id")
    if updates_ch_id:
        updates_ch = guild.get_channel(updates_ch_id)
        if updates_ch:
            try:
                # Find or create a top-level "Tournament Archive" category
                archive_cat = discord.utils.get(guild.categories, name="Tournament Archive")
                if not archive_cat:
                    archive_cat = await guild.create_category("Tournament Archive")

                # Build read-only overwrites: hidden from everyone, staff can view, nobody sends.
                staff_role = guild.get_role(ROLE_ID)
                archive_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, manage_channels=True,
                    ),
                }
                if staff_role:
                    archive_overwrites[staff_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=False, read_message_history=True, add_reactions=False,
                    )

                archive_name = f"results-{t.get('name', t_id).lower().replace(' ', '-')[:80]}"

                await updates_ch.edit(
                    name=archive_name,
                    category=archive_cat,
                    overwrites=archive_overwrites,
                    topic=f"Archived results from {t.get('name', t_id)}",
                )
                print(f"[Archive] Moved tournament-updates to #{archive_name} under 'Tournament Archive'.")
            except Exception as e:
                print(f"[Archive] Failed to archive updates channel: {e}")

    # Delete per-team channels, categories, and roles (SKIP top 2 teams that need VODs)
    for team_id, ch_ids in t.get("team_channels", {}).items():
        if team_id in vod_team_ids:
            # Keep text channel for VOD submissions, delete voice only
            voice_id = ch_ids.get("voice")
            if voice_id:
                ch = guild.get_channel(voice_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
            continue
        for ch_id in [ch_ids.get("text"), ch_ids.get("voice")]:
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
    for team_id, cat_id in t.get("team_categories", {}).items():
        if team_id in vod_team_ids:
            continue  # Keep category for VOD teams
        cat = guild.get_channel(cat_id)
        if cat:
            try:
                await cat.delete()
            except Exception:
                pass
    for team_id, role_id in t.get("team_roles", {}).items():
        if team_id in vod_team_ids:
            continue  # Keep role so pings still work
        role = guild.get_role(role_id)
        if role:
            try:
                await role.delete()
            except Exception:
                pass

    t["open"] = False
    t["ended"] = True
    t.pop("signups_channel_id", None)
    # Keep updates_channel_id so the archived results channel is reachable via the dashboard
    t.pop("admin_channel_id", None)

    # Only remove channel/category/role data for non-VOD teams
    remaining_channels = {tid: ch for tid, ch in t.get("team_channels", {}).items() if tid in vod_team_ids}
    remaining_categories = {tid: cat for tid, cat in t.get("team_categories", {}).items() if tid in vod_team_ids}
    remaining_roles = {tid: r for tid, r in t.get("team_roles", {}).items() if tid in vod_team_ids}
    t["team_channels"] = remaining_channels or None
    t["team_categories"] = remaining_categories or None
    t["team_roles"] = remaining_roles or None
    if not t["team_channels"]:
        t.pop("team_channels", None)
    if not t["team_categories"]:
        t.pop("team_categories", None)
    if not t["team_roles"]:
        t.pop("team_roles", None)
    save_tournaments()

    vod_note = ""
    if vod_team_ids:
        vod_note = f"\nVOD submission embeds sent to the top 2 teams. Their channels are kept open for 1 week."

    await interaction.followup.send(
        f"Tournament `{t_id}` has ended. All channels and roles cleaned up.\n"
        f"Results are preserved -- view them on the dashboard or Challonge: {t.get('challonge_url', 'N/A')}"
        f"{vod_note}"
    )


# ── /tournament-delete ─────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-delete", description="Delete a tournament and its channels.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID to delete")
async def tournament_delete(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can delete this tournament.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    for key in ["signups_channel_id", "updates_channel_id", "matches_channel_id", "admin_channel_id", "bracket_channel_id", "caster_channel_id"]:
        ch_id = t.get(key)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass

    match_ids_to_remove = []
    for mid, match in active_matches.items():
        if match.get("tournament_id") == t_id:
            ch_id = match.get("channel_id")
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
            match_ids_to_remove.append(mid)
    for mid in match_ids_to_remove:
        del active_matches[mid]

    for team_id, ch_ids in t.get("team_channels", {}).items():
        for ch_id in [ch_ids.get("text"), ch_ids.get("voice")]:
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
    for team_id, cat_id in t.get("team_categories", {}).items():
        cat = guild.get_channel(cat_id)
        if cat:
            try:
                await cat.delete()
            except Exception:
                pass
    for team_id, role_id in t.get("team_roles", {}).items():
        role = guild.get_role(role_id)
        if role:
            try:
                await role.delete()
            except Exception:
                pass

    del tournaments[t_id]
    save_tournaments()
    await interaction.followup.send(f"Tournament `{t_id}` has been deleted.")
