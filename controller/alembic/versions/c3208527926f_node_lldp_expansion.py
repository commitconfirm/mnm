"""node_lldp_expansion

Adds 5 nullable columns to ``node_lldp_entries`` for the SNMP-based LLDP
collector (Block C). The NAPALM-path upsert in place today does not
populate them; the SNMP-path adapter landing in Block C P5 will. Existing
rows get NULL on upgrade.

Columns:

- ``local_port_ifindex`` (Integer) — SNMP ifIndex of the local port
- ``local_port_name`` (Text) — resolved from ifXTable.ifName /
  ifTable.ifDescr
- ``remote_chassis_id_subtype`` (Text) — RFC 2922 chassis-id subtype
  enum name (e.g. ``macAddress``, ``interfaceName``)
- ``remote_port_id_subtype`` (Text) — RFC 2922 port-id subtype enum name
- ``remote_system_description`` (Text) — lldpRemSysDesc

Revision ID: c3208527926f
Revises: a1c5c8bbad37
Create Date: 2026-04-24 19:32:35.703474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3208527926f'
down_revision: Union[str, Sequence[str], None] = 'a1c5c8bbad37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "node_lldp_entries",
        sa.Column("local_port_ifindex", sa.Integer(), nullable=True),
    )
    op.add_column(
        "node_lldp_entries",
        sa.Column("local_port_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "node_lldp_entries",
        sa.Column("remote_chassis_id_subtype", sa.Text(), nullable=True),
    )
    op.add_column(
        "node_lldp_entries",
        sa.Column("remote_port_id_subtype", sa.Text(), nullable=True),
    )
    op.add_column(
        "node_lldp_entries",
        sa.Column("remote_system_description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("node_lldp_entries", "remote_system_description")
    op.drop_column("node_lldp_entries", "remote_port_id_subtype")
    op.drop_column("node_lldp_entries", "remote_chassis_id_subtype")
    op.drop_column("node_lldp_entries", "local_port_name")
    op.drop_column("node_lldp_entries", "local_port_ifindex")
