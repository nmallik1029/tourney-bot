"""Per-guild role configuration commands.

Each server configures its own staff/caster/pug roles; the ids are stored per-guild in
config.json (see core/guild_config.py). The original server is seeded from the old env
vars, so it needs no setup. These commands are gated on Discord's Administrator
permission (not the configurable mod role) so a brand-new server can bootstrap before
any role has been set.
"""

import discord
from discord import app_commands

from core.bot_instance import bot
from core.guild_config import set_id, gconf, set_challonge_subdomain, challonge_subdomain


def _admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "Only a server administrator can configure roles.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


@bot.tree.command(
    name="config-roles",
    description="Set this server's tournament staff + caster roles (admin only).",
)
@_admin_only()
@app_commands.describe(
    staff_role="Role allowed to run tournament admin commands (besides administrators)",
    caster_role="Role pinged/allowed for casting (optional)",
)
async def config_roles(
    interaction: discord.Interaction,
    staff_role: discord.Role = None,
    caster_role: discord.Role = None,
):
    gid = interaction.guild.id
    changed = []
    if staff_role is not None:
        set_id("mod_role_id", staff_role.id, gid)
        changed.append(f"Staff: {staff_role.mention}")
    if caster_role is not None:
        set_id("caster_role_id", caster_role.id, gid)
        changed.append(f"Caster: {caster_role.mention}")
    if not changed:
        await interaction.response.send_message("Nothing to change -- pass at least one role.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Updated:\n" + "\n".join(changed), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="config-pug-roles",
    description="Set this server's PUG roles (admin only). Only the roles you pass are changed.",
)
@_admin_only()
@app_commands.describe(
    admin_role="PUG admin role (full access incl. /pug-setup)",
    helper_role="PUG helper role (everything except setup)",
    na_role="Auto-assigned to NA-linked players",
    eu_role="Auto-assigned to EU-linked players",
    captain_role="Captain-priority role (preferred as captains)",
    spectator_role="Spectator/caster role (VC + channel access on matches)",
)
async def config_pug_roles(
    interaction: discord.Interaction,
    admin_role: discord.Role = None,
    helper_role: discord.Role = None,
    na_role: discord.Role = None,
    eu_role: discord.Role = None,
    captain_role: discord.Role = None,
    spectator_role: discord.Role = None,
):
    gid = interaction.guild.id
    mapping = [
        ("pug_admin_role_id", admin_role, "Admin"),
        ("pug_helper_role_id", helper_role, "Helper"),
        ("pug_na_role_id", na_role, "NA"),
        ("pug_eu_role_id", eu_role, "EU"),
        ("pug_captain_role_id", captain_role, "Captain"),
        ("pug_spectator_role_id", spectator_role, "Spectator"),
    ]
    changed = []
    for field, role, label in mapping:
        if role is not None:
            set_id(field, role.id, gid)
            changed.append(f"{label}: {role.mention}")
    if not changed:
        await interaction.response.send_message("Nothing to change -- pass at least one role.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Updated PUG roles:\n" + "\n".join(changed), ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="config-challonge",
    description="Set this server's Challonge Community subdomain for brackets (admin only).",
)
@_admin_only()
@app_commands.describe(
    subdomain="Your Challonge Community subdomain (the X in X.challonge.com). Blank to clear.",
)
async def config_challonge(interaction: discord.Interaction, subdomain: str = ""):
    sub = (subdomain or "").strip().lower().removesuffix(".challonge.com")
    set_challonge_subdomain(sub, interaction.guild.id)
    if sub:
        await interaction.response.send_message(
            f"Brackets for this server will be filed under **{sub}.challonge.com**.\n"
            f"-# The Community must already exist under the bot's Challonge account, or "
            f"bracket creation will fail. Create it at challonge.com if you haven't.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "Cleared. Brackets for this server will go to the account's main page.", ephemeral=True
        )


@bot.tree.command(
    name="config-show",
    description="Show this server's configured roles and categories (admin only).",
)
@_admin_only()
async def config_show(interaction: discord.Interaction):
    c = gconf(interaction.guild.id)

    def rola(rid):
        return f"<@&{rid}>" if rid else "*unset*"

    def cat(cid):
        ch = interaction.guild.get_channel(cid) if cid else None
        return ch.name if ch else "*unset*"

    embed = discord.Embed(title="Server Configuration", color=0x5865F2)
    embed.add_field(
        name="Tournament",
        value=(
            f"Staff role: {rola(c.get('mod_role_id'))}\n"
            f"Caster role: {rola(c.get('caster_role_id'))}\n"
            f"Tournament category: {cat(c.get('tournament_category_id'))}\n"
            f"Admin category: {cat(c.get('admin_category_id'))}"
        ),
        inline=False,
    )
    embed.add_field(
        name="PUG",
        value=(
            f"Admin: {rola(c.get('pug_admin_role_id'))}\n"
            f"Helper: {rola(c.get('pug_helper_role_id'))}\n"
            f"NA: {rola(c.get('pug_na_role_id'))} | EU: {rola(c.get('pug_eu_role_id'))}\n"
            f"Captain: {rola(c.get('pug_captain_role_id'))}\n"
            f"Spectator: {rola(c.get('pug_spectator_role_id'))}"
        ),
        inline=False,
    )
    sub = challonge_subdomain(interaction.guild.id)
    embed.add_field(
        name="Challonge",
        value=(f"Community: **{sub}.challonge.com**" if sub else "Community: *main account*"),
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )
