import uuid
import discord
from core.storage import tournaments, save_tournaments, pending_registrations
from utils.helpers import build_team_embed


class SignupView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Register Your Team", style=discord.ButtonStyle.success, custom_id="register_team_btn")
    async def register_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        t_id = self.tournament_id
        if t_id not in tournaments:
            topic = interaction.channel.topic or ""
            for tid, t in tournaments.items():
                if tid in topic:
                    t_id = tid
                    break
            else:
                await interaction.response.send_message("Tournament not found. It may have already closed.", ephemeral=True)
                return

        t = tournaments[t_id]
        if not t["open"]:
            await interaction.response.send_message("Sign-ups for this tournament are closed.", ephemeral=True)
            return

        team_size = t["team_size"]
        view = PlayerSelectView(tournament_id=t_id, team_size=team_size)
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        instructions = "\n".join(f"**Slot {i+1} -- {label}**" for i, label in enumerate(player_labels))

        await interaction.response.send_message(
            f"**Select your team members:**\n{instructions}\n\nUse the menus below to select each player, then click Submit.",
            view=view,
            ephemeral=True,
        )


class PlayerSelectView(discord.ui.View):
    def __init__(self, tournament_id: str, team_size: int):
        super().__init__(timeout=300)
        self.tournament_id = tournament_id
        self.team_size = team_size
        self.selected_users: dict[int, discord.Member] = {}
        self.selected_coach: discord.Member | None = None
        self.selected_subs: dict[int, discord.Member] = {}

        # Max 5 rows on a View; button takes 1; required players take team_size.
        # So we have (4 - team_size) rows to spend on optional slots.
        available = 4 - team_size
        self.include_coach = available >= 1
        self.include_sub1 = available >= 2
        self.include_sub2 = available >= 3

        row = 0
        if self.include_coach:
            self.add_item(CoachUserSelect(row=row))
            row += 1

        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels):
            self.add_item(PlayerUserSelect(slot_index=i, label=label, row=row))
            row += 1

        if self.include_sub1:
            self.add_item(SubUserSelect(slot_index=0, label="Sub 1", row=row))
            row += 1
        if self.include_sub2:
            self.add_item(SubUserSelect(slot_index=1, label="Sub 2", row=row))
            row += 1

    @discord.ui.button(label="Next: Enter IGNs", style=discord.ButtonStyle.primary, row=4)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.selected_users) < self.team_size:
            await interaction.response.send_message(
                f"Please select all {self.team_size} players before continuing.",
                ephemeral=True,
            )
            return

        # Prevent the same user from being both a required player and coach/sub
        required_ids = {m.id for m in self.selected_users.values()}
        optional_members = []
        if self.selected_coach:
            optional_members.append(("coach", self.selected_coach))
        for idx, m in self.selected_subs.items():
            optional_members.append((f"sub{idx+1}", m))

        optional_ids = [m.id for _, m in optional_members]
        if any(oid in required_ids for oid in optional_ids):
            await interaction.response.send_message(
                "A Coach or Sub can't also be one of your required players. Please pick different users.",
                ephemeral=True,
            )
            return
        if len(set(optional_ids)) != len(optional_ids):
            await interaction.response.send_message(
                "You picked the same user for multiple optional slots. Please pick unique users.",
                ephemeral=True,
            )
            return

        pending_registrations[interaction.user.id] = {
            "tournament_id": self.tournament_id,
            "selected_users": {i: m for i, m in self.selected_users.items()},
        }

        modal = IGNModal(
            tournament_id=self.tournament_id,
            selected_users=self.selected_users,
            team_size=self.team_size,
            selected_coach=self.selected_coach,
            selected_subs=self.selected_subs,
        )
        await interaction.response.send_modal(modal)


class PlayerUserSelect(discord.ui.UserSelect):
    def __init__(self, slot_index: int, label: str, row: int | None = None):
        # Fall back to legacy behavior (row = slot_index capped at 3) when not given
        if row is None:
            row = min(slot_index, 3)
        super().__init__(
            placeholder=f"Select {label}...",
            row=row,
        )
        self.slot_index = slot_index

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_users[self.slot_index] = self.values[0]
        await interaction.response.defer()


class CoachUserSelect(discord.ui.UserSelect):
    def __init__(self, row: int):
        super().__init__(
            placeholder="Select Coach (optional)...",
            row=row,
            min_values=0,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_coach = self.values[0] if self.values else None
        await interaction.response.defer()


class SubUserSelect(discord.ui.UserSelect):
    def __init__(self, slot_index: int, label: str, row: int):
        super().__init__(
            placeholder=f"Select {label} (optional)...",
            row=row,
            min_values=0,
            max_values=1,
        )
        self.slot_index = slot_index

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            self.view.selected_subs[self.slot_index] = self.values[0]
        else:
            self.view.selected_subs.pop(self.slot_index, None)
        await interaction.response.defer()


class IGNModal(discord.ui.Modal, title="Enter Team Name & IGNs"):
    def __init__(
        self,
        tournament_id: str,
        selected_users: dict,
        team_size: int,
        selected_coach: discord.Member | None = None,
        selected_subs: dict | None = None,
    ):
        super().__init__()
        self.tournament_id = tournament_id
        self.selected_users = selected_users
        self.team_size = team_size
        self.selected_coach = selected_coach
        self.selected_subs = selected_subs or {}

        self.team_name_field = discord.ui.TextInput(
            label="Team Name",
            placeholder="Enter your team name",
            max_length=50,
        )
        self.add_item(self.team_name_field)

        # Modal max = 5 inputs. Budget: team_name + player IGNs + optional IGNs.
        remaining = 5 - 1 - team_size  # after team name + required player IGNs

        self.ign_fields = []
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels[:4]):
            field = discord.ui.TextInput(
                label=f"{label} IGN",
                placeholder=f"In-game name for {selected_users.get(i, 'this player')}",
                max_length=32,
            )
            self.add_item(field)
            self.ign_fields.append((i, label, field))

        # Optional IGN fields (only added if there's room AND the user selected them)
        self.coach_field = None
        if selected_coach and remaining >= 1:
            self.coach_field = discord.ui.TextInput(
                label="Coach IGN (optional)",
                placeholder=f"In-game name for {selected_coach}",
                max_length=32,
                required=False,
            )
            self.add_item(self.coach_field)
            remaining -= 1

        self.sub_fields: list[tuple[int, discord.ui.TextInput]] = []
        for sub_idx in sorted(self.selected_subs.keys()):
            if remaining <= 0:
                break
            member = self.selected_subs[sub_idx]
            field = discord.ui.TextInput(
                label=f"Sub {sub_idx + 1} IGN (optional)",
                placeholder=f"In-game name for {member}",
                max_length=32,
                required=False,
            )
            self.add_item(field)
            self.sub_fields.append((sub_idx, field))
            remaining -= 1

    async def on_submit(self, interaction: discord.Interaction):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        # Respond immediately so Discord doesn't time out
        team_name = self.team_name_field.value.strip()
        await interaction.response.send_message(
            f"**Team '{team_name}' registered!** You'll be notified when the tournament starts.",
            ephemeral=True,
        )

        players = []
        for i, label, field in self.ign_fields:
            member = self.selected_users.get(i)
            if not member:
                continue
            players.append({
                "label": label,
                "discord_id": member.id,
                "ign": field.value.strip(),
            })

        coach = None
        if self.selected_coach:
            coach = {
                "label": "Coach",
                "discord_id": self.selected_coach.id,
                "ign": self.coach_field.value.strip() if self.coach_field else "",
            }

        subs = []
        sub_ign_map = {idx: field.value.strip() for idx, field in self.sub_fields}
        for sub_idx in sorted(self.selected_subs.keys()):
            member = self.selected_subs[sub_idx]
            subs.append({
                "label": f"Sub {sub_idx + 1}",
                "discord_id": member.id,
                "ign": sub_ign_map.get(sub_idx, ""),
            })

        team_entry = {
            "team_id": str(uuid.uuid4()),
            "team_name": team_name,
            "submitted_by": interaction.user.id,
            "submitter_name": interaction.user.name,
            "signup_message_id": None,
            "players": players,
            "coach": coach,
            "subs": subs,
            "submitted_at": str(discord.utils.utcnow()),
        }
        tournaments[self.tournament_id]["teams"].append(team_entry)
        save_tournaments()

        signups_ch = interaction.guild.get_channel(t["signups_channel_id"])
        if signups_ch:
            embed = build_team_embed(team_entry, self.tournament_id, interaction.user.name)
            view = EditRosterView(team_id=team_entry["team_id"], tournament_id=self.tournament_id)
            msg = await signups_ch.send(embed=embed, view=view)
            team_entry["signup_message_id"] = msg.id

            # Delete old sign-up button and repost at bottom
            old_btn_id = t.get("signup_button_message_id")
            if old_btn_id:
                try:
                    old_msg = await signups_ch.fetch_message(old_btn_id)
                    await old_msg.delete()
                except Exception:
                    pass

            signup_embed = discord.Embed(
                title=f"{t['name']}  --  Registration",
                description=f"Click the button below to register for the **{t['name']}**!",
                color=0x00FF7F,
            )
            signup_embed.add_field(name="Prizes", value=t.get("prizes", "N/A"), inline=True)
            signup_embed.add_field(name="Format", value=t.get("format", "N/A").upper(), inline=True)
            signup_embed.add_field(name="Date", value=t.get("date", "N/A"), inline=True)
            signup_embed.set_footer(text=f"Tournament ID: {self.tournament_id}")

            new_btn_msg = await signups_ch.send(embed=signup_embed, view=SignupView(tournament_id=self.tournament_id))
            t["signup_button_message_id"] = new_btn_msg.id
            save_tournaments()


class EditRosterView(discord.ui.View):
    def __init__(self, team_id: str, tournament_id: str):
        super().__init__(timeout=None)
        self.team_id = team_id
        self.tournament_id = tournament_id

    async def _validate(self, interaction: discord.Interaction):
        """Returns (tournament, team) if the user can edit, else None after sending a response."""
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return None, None

        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return None, None

        if interaction.user.id != team["submitted_by"]:
            await interaction.response.send_message("Only the person who registered this team can edit it.", ephemeral=True)
            return None, None

        if not t["open"]:
            await interaction.response.send_message("Sign-ups are closed. The roster can no longer be edited.", ephemeral=True)
            return None, None

        return t, team

    @discord.ui.button(label="Edit Roster", style=discord.ButtonStyle.primary, custom_id="edit_roster_btn")
    async def edit_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        t, team = await self._validate(interaction)
        if not t:
            return

        view = EditPlayerSelectView(
            tournament_id=self.tournament_id,
            team_id=self.team_id,
            team_size=t["team_size"],
        )
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, t["team_size"] + 1)]
        instructions = "\n".join(f"**Slot {i+1} -- {label}**" for i, label in enumerate(player_labels))

        await interaction.response.send_message(
            f"**Update your team members:**\n{instructions}\n\nSelect new players and click Next.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Manage Coach/Subs", style=discord.ButtonStyle.secondary, custom_id="manage_coach_subs_btn")
    async def manage_coach_subs(self, interaction: discord.Interaction, button: discord.ui.Button):
        t, team = await self._validate(interaction)
        if not t:
            return

        view = ManageCoachSubsView(
            tournament_id=self.tournament_id,
            team_id=self.team_id,
        )

        current_lines = []
        coach = team.get("coach")
        if coach:
            ign_part = f" ({coach['ign']})" if coach.get("ign") else ""
            current_lines.append(f"**Coach:** <@{coach['discord_id']}>{ign_part}")
        else:
            current_lines.append("**Coach:** *(none)*")
        subs = team.get("subs") or []
        for i in range(2):
            if i < len(subs):
                s = subs[i]
                ign_part = f" ({s['ign']})" if s.get("ign") else ""
                current_lines.append(f"**Sub {i+1}:** <@{s['discord_id']}>{ign_part}")
            else:
                current_lines.append(f"**Sub {i+1}:** *(none)*")

        await interaction.response.send_message(
            "**Manage Coach & Subs**\n"
            "Select users for any slot you want to fill. Leave a slot empty to clear it.\n\n"
            + "\n".join(current_lines)
            + "\n\nClick **Save** when done.",
            view=view,
            ephemeral=True,
        )


# ── Coach/Subs management flow ────────────────────────────────────────────────
class ManageCoachSubsView(discord.ui.View):
    def __init__(self, tournament_id: str, team_id: str):
        super().__init__(timeout=300)
        self.tournament_id = tournament_id
        self.team_id = team_id
        # None = unchanged, False = cleared, Member = set to new user
        self.selected_coach = None
        self.selected_subs: dict[int, discord.Member | bool] = {}

        self.add_item(ManageCoachSelect(row=0))
        self.add_item(ManageSubSelect(slot_index=0, label="Sub 1", row=1))
        self.add_item(ManageSubSelect(slot_index=1, label="Sub 2", row=2))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return
        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return

        # Validate uniqueness vs required roster
        required_ids = {p["discord_id"] for p in team.get("players", [])}
        new_optional_ids = []
        if isinstance(self.selected_coach, discord.Member):
            new_optional_ids.append(self.selected_coach.id)
        for m in self.selected_subs.values():
            if isinstance(m, discord.Member):
                new_optional_ids.append(m.id)
        if any(oid in required_ids for oid in new_optional_ids):
            await interaction.response.send_message(
                "A Coach or Sub can't also be one of your required players. Please pick different users.",
                ephemeral=True,
            )
            return
        if len(set(new_optional_ids)) != len(new_optional_ids):
            await interaction.response.send_message(
                "You picked the same user for multiple optional slots. Please pick unique users.",
                ephemeral=True,
            )
            return

        # Determine which IGN fields we need to ask for
        ign_prompts: list[tuple[str, str, discord.Member, str]] = []
        # (slot_key, label, member, existing_ign)

        # Coach
        if isinstance(self.selected_coach, discord.Member):
            existing_ign = (team.get("coach") or {}).get("ign", "") if (team.get("coach") or {}).get("discord_id") == self.selected_coach.id else ""
            ign_prompts.append(("coach", "Coach IGN (optional)", self.selected_coach, existing_ign))

        # Subs
        existing_subs = team.get("subs") or []
        for idx in sorted(self.selected_subs.keys()):
            val = self.selected_subs[idx]
            if isinstance(val, discord.Member):
                existing_ign = ""
                if idx < len(existing_subs) and existing_subs[idx].get("discord_id") == val.id:
                    existing_ign = existing_subs[idx].get("ign", "")
                ign_prompts.append((f"sub{idx}", f"Sub {idx+1} IGN (optional)", val, existing_ign))

        if ign_prompts:
            # Ask for IGNs via modal
            modal = ManageCoachSubsIGNModal(
                tournament_id=self.tournament_id,
                team_id=self.team_id,
                selected_coach=self.selected_coach,
                selected_subs=self.selected_subs,
                ign_prompts=ign_prompts,
            )
            await interaction.response.send_modal(modal)
            return

        # Nothing requires an IGN — just apply (clears/no-op)
        apply_coach_sub_changes(
            team=team,
            selected_coach=self.selected_coach,
            selected_subs=self.selected_subs,
            new_igns={},
        )
        save_tournaments()

        await _refresh_team_embed(interaction, t, team, self.tournament_id, self.team_id)
        await interaction.response.send_message(
            f"Coach/Subs updated for **{team['team_name']}**.",
            ephemeral=True,
        )


class ManageCoachSelect(discord.ui.UserSelect):
    def __init__(self, row: int):
        super().__init__(
            placeholder="Coach (leave empty to clear)...",
            row=row,
            min_values=0,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        # Empty selection -> clear
        self.view.selected_coach = self.values[0] if self.values else False
        await interaction.response.defer()


class ManageSubSelect(discord.ui.UserSelect):
    def __init__(self, slot_index: int, label: str, row: int):
        super().__init__(
            placeholder=f"{label} (leave empty to clear)...",
            row=row,
            min_values=0,
            max_values=1,
        )
        self.slot_index = slot_index

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_subs[self.slot_index] = self.values[0] if self.values else False
        await interaction.response.defer()


class ManageCoachSubsIGNModal(discord.ui.Modal, title="Coach/Subs IGNs"):
    def __init__(self, tournament_id, team_id, selected_coach, selected_subs, ign_prompts):
        super().__init__()
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.selected_coach = selected_coach
        self.selected_subs = selected_subs

        self.fields: list[tuple[str, discord.ui.TextInput]] = []
        for key, label, member, existing in ign_prompts[:5]:
            field = discord.ui.TextInput(
                label=label,
                placeholder=f"In-game name for {member}",
                default=existing,
                max_length=32,
                required=False,
            )
            self.add_item(field)
            self.fields.append((key, field))

    async def on_submit(self, interaction: discord.Interaction):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return
        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return

        new_igns = {key: field.value.strip() for key, field in self.fields}

        apply_coach_sub_changes(
            team=team,
            selected_coach=self.selected_coach,
            selected_subs=self.selected_subs,
            new_igns=new_igns,
        )
        save_tournaments()

        await _refresh_team_embed(interaction, t, team, self.tournament_id, self.team_id)
        await interaction.response.send_message(
            f"Coach/Subs updated for **{team['team_name']}**.",
            ephemeral=True,
        )


def apply_coach_sub_changes(team: dict, selected_coach, selected_subs: dict, new_igns: dict):
    """Apply coach/sub changes to the team dict in-place.

    selected_coach is: None (unchanged), False (clear), or a Member (set).
    selected_subs values same semantics.
    new_igns is a dict keyed by "coach"/"sub0"/"sub1" with the IGN strings.
    """
    # Coach
    if selected_coach is False:
        team["coach"] = None
    elif isinstance(selected_coach, discord.Member):
        team["coach"] = {
            "label": "Coach",
            "discord_id": selected_coach.id,
            "ign": new_igns.get("coach", ""),
        }

    # Subs
    current_subs = team.get("subs") or []
    sub_map: dict[int, dict | None] = {}
    for i, sub in enumerate(current_subs[:2]):
        sub_map[i] = sub

    for idx, val in selected_subs.items():
        if val is False:
            sub_map[idx] = None
        elif isinstance(val, discord.Member):
            sub_map[idx] = {
                "label": f"Sub {idx+1}",
                "discord_id": val.id,
                "ign": new_igns.get(f"sub{idx}", ""),
            }

    # Compact: keep only non-None entries, but preserve slot ordering
    new_subs = []
    for idx in sorted(sub_map.keys()):
        if sub_map[idx] is not None:
            new_subs.append(sub_map[idx])
    team["subs"] = new_subs


async def _refresh_team_embed(interaction, t, team, tournament_id, team_id):
    """Re-render the team's signup embed with updated data."""
    signups_ch = interaction.guild.get_channel(t.get("signups_channel_id"))
    if signups_ch and team.get("signup_message_id"):
        try:
            original_msg = await signups_ch.fetch_message(team["signup_message_id"])
            updated_embed = build_team_embed(team, tournament_id, team["submitter_name"])
            view = EditRosterView(team_id=team_id, tournament_id=tournament_id)
            await original_msg.edit(embed=updated_embed, view=view)
        except discord.NotFound:
            pass


class EditPlayerSelectView(discord.ui.View):
    def __init__(self, tournament_id: str, team_id: str, team_size: int):
        super().__init__(timeout=300)
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.team_size = team_size
        self.selected_users: dict[int, discord.Member] = {}

        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels):
            self.add_item(PlayerUserSelect(slot_index=i, label=label))

    @discord.ui.button(label="Next: Update IGNs", style=discord.ButtonStyle.primary, row=4)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.selected_users) < self.team_size:
            await interaction.response.send_message(
                f"Please select all {self.team_size} players before continuing.",
                ephemeral=True,
            )
            return

        modal = EditIGNModal(
            tournament_id=self.tournament_id,
            team_id=self.team_id,
            selected_users=self.selected_users,
            team_size=self.team_size,
        )
        await interaction.response.send_modal(modal)


class EditIGNModal(discord.ui.Modal, title="Update Team Name & IGNs"):
    def __init__(self, tournament_id: str, team_id: str, selected_users: dict, team_size: int):
        super().__init__()
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.selected_users = selected_users
        self.team_size = team_size

        t = tournaments.get(tournament_id, {})
        team = next((tm for tm in t.get("teams", []) if tm["team_id"] == team_id), {})

        self.team_name_field = discord.ui.TextInput(
            label="Team Name",
            placeholder="Enter new team name",
            default=team.get("team_name", ""),
            max_length=50,
        )
        self.add_item(self.team_name_field)

        self.ign_fields = []
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels[:4]):
            existing_ign = ""
            if team.get("players") and i < len(team["players"]):
                existing_ign = team["players"][i].get("ign", "")
            field = discord.ui.TextInput(
                label=f"{label} IGN",
                placeholder=f"IGN for {selected_users.get(i, 'this player')}",
                default=existing_ign,
                max_length=32,
            )
            self.add_item(field)
            self.ign_fields.append((i, label, field))

    async def on_submit(self, interaction: discord.Interaction):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return

        team["team_name"] = self.team_name_field.value.strip()
        new_players = []
        for i, label, field in self.ign_fields:
            member = self.selected_users.get(i)
            if not member:
                continue
            new_players.append({
                "label": label,
                "discord_id": member.id,
                "ign": field.value.strip(),
            })

        team["players"] = new_players
        save_tournaments()

        signups_ch = interaction.guild.get_channel(t["signups_channel_id"])
        if signups_ch and team.get("signup_message_id"):
            try:
                original_msg = await signups_ch.fetch_message(team["signup_message_id"])
                updated_embed = build_team_embed(team, self.tournament_id, team["submitter_name"])
                view = EditRosterView(team_id=self.team_id, tournament_id=self.tournament_id)
                await original_msg.edit(embed=updated_embed, view=view)
            except discord.NotFound:
                pass

        await interaction.response.send_message(
            f"Roster for **{team['team_name']}** updated successfully!",
            ephemeral=True,
        )
