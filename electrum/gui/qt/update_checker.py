# Copyright (C) 2019 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

import asyncio
from distutils.version import StrictVersion

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QProgressBar,
                             QHBoxLayout, QPushButton, QDialog)

from electrum import version
from electrum.i18n import _
from electrum.util import make_aiohttp_session
from electrum.logging import Logger
from electrum.network import Network


class UpdateCheck(QDialog, Logger):
    # Electrum-DOI is released on GitHub, so that is where the version check has
    # to look. Inheriting upstream's electrum.org endpoints was not merely wrong
    # but harmful: https://electrum.org/version is a genuine, correctly signed
    # announcement -- of the Bitcoin Electrum version. It verified, it compared
    # greater than every Electrum-DOI version, and the wallet then offered
    # "Update to Electrum-DOI 4.6.x is available", linking to the download page
    # of a different wallet.
    #
    # Upstream signs that announcement so a compromised electrum.org cannot make
    # installed wallets advertise an update. The check is dropped here rather
    # than reproduced with a Doichain key, because announcement and binaries
    # would both come from GitHub: one origin, so a signature verified against a
    # key shipped in the same repository buys little. Getting the property back
    # needs a signing key kept outside GitHub -- tracked separately.
    url = "https://api.github.com/repos/Doichain/electrum-doi/releases/latest"
    download_url = "https://github.com/Doichain/electrum-doi/releases/latest"

    # Release tags read dc4.1.6, dc4.1.5, ...; the version is the tag without it.
    TAG_PREFIX = "dc"

    def __init__(self, *, latest_version=None):
        QDialog.__init__(self)
        self.setWindowTitle('Electrum-DOI - ' + _('Update Check'))
        self.content = QVBoxLayout()
        self.content.setContentsMargins(*[10]*4)

        self.heading_label = QLabel()
        self.content.addWidget(self.heading_label)

        self.detail_label = QLabel()
        self.detail_label.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.detail_label.setOpenExternalLinks(True)
        self.content.addWidget(self.detail_label)

        self.pb = QProgressBar()
        self.pb.setMaximum(0)
        self.pb.setMinimum(0)
        self.content.addWidget(self.pb)

        versions = QHBoxLayout()
        versions.addWidget(QLabel(_("Current version: {}".format(version.ELECTRUM_VERSION))))
        self.latest_version_label = QLabel(_("Latest version: {}".format(" ")))
        versions.addWidget(self.latest_version_label)
        self.content.addLayout(versions)

        self.update_view(latest_version)

        self.update_check_thread = UpdateCheckThread()
        self.update_check_thread.checked.connect(self.on_version_retrieved)
        self.update_check_thread.failed.connect(self.on_retrieval_failed)
        self.update_check_thread.start()

        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.close)
        self.content.addWidget(close_button)
        self.setLayout(self.content)
        self.show()

    def on_version_retrieved(self, version):
        self.update_view(version)

    def on_retrieval_failed(self):
        self.heading_label.setText('<h2>' + _("Update check failed") + '</h2>')
        self.detail_label.setText(_("Sorry, but we were unable to check for updates. Please try again later."))
        self.pb.hide()

    @staticmethod
    def is_newer(latest_version):
        return latest_version > StrictVersion(version.ELECTRUM_VERSION)

    def update_view(self, latest_version=None):
        if latest_version:
            self.pb.hide()
            self.latest_version_label.setText(_("Latest version: {}".format(latest_version)))
            if self.is_newer(latest_version):
                self.heading_label.setText('<h2>' + _("There is a new update available") + '</h2>')
                url = "<a href='{u}'>{u}</a>".format(u=UpdateCheck.download_url)
                self.detail_label.setText(_("You can download the new version from {}.").format(url))
            else:
                self.heading_label.setText('<h2>' + _("Already up to date") + '</h2>')
                self.detail_label.setText(_("You are already on the latest version of Electrum-DOI."))
        else:
            self.heading_label.setText('<h2>' + _("Checking for updates...") + '</h2>')
            self.detail_label.setText(_("Please wait while Electrum-DOI checks for available updates."))


class UpdateCheckThread(QThread, Logger):
    checked = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self):
        QThread.__init__(self)
        Logger.__init__(self)
        self.network = Network.get_instance()

    async def get_update_info(self):
        # note: Use long timeout here as it is not critical that we get a response fast,
        #       and it's bad not to get an update notification just because we did not wait enough.
        async with make_aiohttp_session(proxy=self.network.proxy, timeout=120) as session:
            headers = {'Accept': 'application/vnd.github+json'}
            async with session.get(UpdateCheck.url, headers=headers) as result:
                release = await result.json(content_type=None)
                # GitHub's "latest" already excludes drafts and prereleases, so
                # whatever it names is published and has downloadable assets.
                tag_name = release['tag_name']
                self.logger.info(f"latest release on GitHub is tagged '{tag_name}'")
                version_num = tag_name
                if version_num.startswith(UpdateCheck.TAG_PREFIX):
                    version_num = version_num[len(UpdateCheck.TAG_PREFIX):]
                # StrictVersion takes two or three components and raises on a
                # fourth, which the old dc4.1.4.3 tags had. A tag that does not
                # parse surfaces as a failed check, not as a bogus update prompt.
                return StrictVersion(version_num.strip())

    def run(self):
        if not self.network:
            self.failed.emit()
            return
        try:
            update_info = asyncio.run_coroutine_threadsafe(self.get_update_info(), self.network.asyncio_loop).result()
        except Exception as e:
            self.logger.info(f"got exception: '{repr(e)}'")
            self.failed.emit()
        else:
            self.checked.emit(update_info)
