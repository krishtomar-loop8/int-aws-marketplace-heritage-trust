"""Microsoft Graph read-only connector (SharePoint/M365 + Outlook).

App-only (client-credentials) access to Microsoft Graph for the matter-search
spec: correspondence (mail), documents (SharePoint), and calendar events. All
data methods are GETs; the connector never writes to M365.
"""

from __future__ import annotations

from .connector import GraphConnector

__all__ = ["GraphConnector"]
