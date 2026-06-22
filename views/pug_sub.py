import discord

from core.guild_views import GuildView
from pug.config import member_is_pug_staff
from pug.storage import pug_matches, get_player, is_noadded


class RequestSubButton(discord.ui.Button):
    """On the draft, vote, and host views. Any player in the match (or an admin)
    can start a sub request."""
    def __init__(self, match_key: str):
        super().__init__(
            label="Request a Sub",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pugsub_{match_key}",
            row=4,
        )
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") not in ("draft", "vote", "host"):
            await interaction.response.send_message("Subs aren't available right now.", ephemeral=True)
            return
        if interaction.user.id not in match.get("players", []) and not member_is_pug_staff(interaction.user):
            await interaction.response.send_message(
                "Only players in this match (or an admin) can request a sub.", ephemeral=True
            )
            return
        if match.get("pending_sub"):
            await interaction.response.send_message("A sub request is already pending.", ephemeral=True)
            return

        is_staff = member_is_pug_staff(interaction.user)
        hint = "Pick who to sub **out**, then search for the player to sub **in**, then click Confirm."
        if is_staff:
            hint += "\n*(Staff: **Force Sub** skips the 4-way approval.)*"
        await interaction.response.send_message(
            hint,
            view=SubSetupView(self.match_key, interaction.guild, interaction.user.id, is_staff),
            ephemeral=True,
        )


class SubSetupView(GuildView):
    def __init__(self, match_key: str, guild: discord.Guild, requester_id: int, is_staff: bool = False):
        super().__init__(timeout=120)
        self.match_key = match_key
        self.out_id: int | None = None
        self.in_id: int | None = None

        match = pug_matches[match_key]
        options = []
        for pid in match["players"]:
            name = match.get("names", {}).get(pid) or str(pid)
            if pid in match.get("captains", []):
                name += " (captain)"
            # Default the "out" selection to the requester for convenience
            options.append(discord.SelectOption(
                label=name[:100], value=str(pid), default=(pid == requester_id)
            ))
        if requester_id in match["players"]:
            self.out_id = requester_id

        self.add_item(OutgoingSelect(options))
        self.add_item(IncomingSelect())
        self.add_item(ConfirmSubButton())
        if is_staff:
            self.add_item(ForceSubButton())


def _validate_sub(match: dict, out_id, in_id, guild):
    """Returns (error_message_or_None, in_member_or_None)."""
    if not out_id or not in_id:
        return "Select both players first.", None
    if in_id == out_id or in_id in match["players"]:
        return "That player is already in this match.", None
    if is_noadded(in_id):
        return "That player is no-added.", None
    if not get_player(in_id)["usernames"]:
        return "The incoming player needs a linked Krunker account first (`/link`).", None
    in_member = guild.get_member(in_id)
    if not in_member:
        return "Couldn't find that member.", None
    return None, in_member


class ForceSubButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Force Sub (Staff)", style=discord.ButtonStyle.danger, row=3)

    async def callback(self, interaction: discord.Interaction):
        view: SubSetupView = self.view
        match = pug_matches.get(view.match_key)
        if not match:
            await interaction.response.send_message("Match no longer exists.", ephemeral=True)
            return
        if not member_is_pug_staff(interaction.user):
            await interaction.response.send_message("Only staff can force a sub.", ephemeral=True)
            return
        err, _ = _validate_sub(match, view.out_id, view.in_id, interaction.guild)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        out_id, in_id = view.out_id, view.in_id
        match.pop("pending_sub", None)
        await interaction.response.edit_message(content="Forcing sub...", view=None)
        from pug.match import replace_player
        await replace_player(match, out_id, in_id, interaction.client)
        await interaction.channel.send(
            f"{interaction.user.mention} force-subbed <@{out_id}> ➜ <@{in_id}>.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )


class OutgoingSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Player to sub OUT", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.out_id = int(self.values[0])
        await interaction.response.defer()


class IncomingSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Search a player to sub IN...", row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.in_id = self.values[0].id
        await interaction.response.defer()


class ConfirmSubButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Confirm", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: SubSetupView = self.view
        match = pug_matches.get(view.match_key)
        if not match:
            await interaction.response.send_message("Match no longer exists.", ephemeral=True)
            return
        if match.get("pending_sub"):
            await interaction.response.send_message("A sub request is already pending.", ephemeral=True)
            return
        err, in_member = _validate_sub(match, view.out_id, view.in_id, interaction.guild)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        out_id, in_id = view.out_id, view.in_id

        # Give the incoming player access to the match channel so they can approve.
        ch = interaction.channel
        try:
            await ch.set_permissions(in_member, view_channel=True, send_messages=True)
        except discord.HTTPException:
            pass

        cap1, cap2 = match["captains"]
        names = match.get("names", {})

        def nm(uid):
            if uid == in_id:
                return in_member.display_name
            return names.get(uid, str(uid))

        slots = [
            {"role": "Captain 1", "uid": cap1, "name": nm(cap1), "approved": False},
            {"role": "Captain 2", "uid": cap2, "name": nm(cap2), "approved": False},
            {"role": "Subbing Out", "uid": out_id, "name": nm(out_id), "approved": False},
            {"role": "Subbing In", "uid": in_id, "name": nm(in_id), "approved": False},
        ]
        match["pending_sub"] = {"out": out_id, "in": in_id, "by": interaction.user.id, "slots": slots}

        await interaction.response.edit_message(content="Sub request sent.", view=None)
        await ch.send(
            f"<@{cap1}> <@{cap2}> <@{out_id}> <@{in_id}>",
            embed=_sub_embed(match),
            view=SubApprovalView(view.match_key),
            allowed_mentions=discord.AllowedMentions(users=True),
        )


def _sub_embed(match: dict) -> discord.Embed:
    sub = match["pending_sub"]
    lines = []
    for s in sub["slots"]:
        mark = "Approved" if s["approved"] else "Pending"
        lines.append(f"{mark}: **{s['role']}** | <@{s['uid']}>")
    embed = discord.Embed(
        title="Sub Request",
        description=(
            f"**Out:** <@{sub['out']}>\n"
            f"**In:** <@{sub['in']}>\n\n"
            f"All four must approve:\n" + "\n".join(lines)
        ),
        color=0xFFA500,
    )
    return embed


class SubApprovalView(GuildView):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        match = pug_matches.get(match_key)
        sub = match.get("pending_sub") if match else None
        if sub:
            for i, s in enumerate(sub["slots"]):
                self.add_item(SubSlotButton(match_key, i, s))
        self.add_item(SubDenyButton(match_key))


class SubSlotButton(discord.ui.Button):
    def __init__(self, match_key: str, index: int, slot: dict):
        super().__init__(
            label=(slot["role"] if slot["approved"] else f"{slot['role']}: {slot['name']}")[:80],
            style=discord.ButtonStyle.secondary if slot["approved"] else discord.ButtonStyle.success,
            custom_id=f"pugsubok_{match_key}_{index}",
            disabled=slot["approved"],
        )
        self.match_key = match_key
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        sub = match.get("pending_sub") if match else None
        if not sub:
            await interaction.response.send_message("No pending sub request.", ephemeral=True)
            return

        slot = sub["slots"][self.index]
        if interaction.user.id != slot["uid"] and not member_is_pug_staff(interaction.user):
            await interaction.response.send_message(
                f"Only <@{slot['uid']}> can approve the **{slot['role']}** slot.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        slot["approved"] = True

        if all(s["approved"] for s in sub["slots"]):
            out_id, in_id = sub["out"], sub["in"]
            from pug.match import replace_player
            await replace_player(match, out_id, in_id, interaction.client)
            match.pop("pending_sub", None)
            await interaction.response.edit_message(
                content=f"Sub approved: <@{out_id}> -> <@{in_id}>",
                embed=None,
                view=None,
            )
        else:
            view = SubApprovalView(self.match_key)
            await interaction.response.edit_message(embed=_sub_embed(match), view=view)


class SubDenyButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"pugsubno_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        sub = match.get("pending_sub") if match else None
        if not sub:
            await interaction.response.send_message("No pending sub request.", ephemeral=True)
            return
        allowed_ids = {s["uid"] for s in sub["slots"]}
        if interaction.user.id not in allowed_ids and not member_is_pug_staff(interaction.user):
            await interaction.response.send_message("Only an involved party or an admin can deny.", ephemeral=True)
            return

        in_id = sub["in"]
        match.pop("pending_sub", None)

        # Revoke the incoming player's temporary channel access (sub didn't happen)
        in_member = interaction.guild.get_member(in_id)
        if in_member:
            try:
                await interaction.channel.set_permissions(in_member, overwrite=None)
            except discord.HTTPException:
                pass

        await interaction.response.edit_message(
            content=f"Sub request denied by <@{interaction.user.id}>.",
            embed=None,
            view=None,
        )
