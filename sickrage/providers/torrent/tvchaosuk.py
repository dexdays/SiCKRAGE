# This file is part of SickRage.
#
# SickRage is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickRage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickRage.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import re

from requests.utils import dict_from_cookiejar

import sickrage
from sickrage.core.caches.tv_cache import TVCache
from sickrage.core.exceptions import AuthException
from sickrage.core.helpers import sanitizeSceneName, show_names, bs4_parser, try_int, convert_size
from sickrage.providers import TorrentProvider


class TVChaosUKProvider(TorrentProvider):
    def __init__(self):
        super(TVChaosUKProvider, self).__init__('TvChaosUK', 'https://www.tvchaosuk.com', True)

        self.urls.update({
            'login': '{base_url}/takelogin.php'.format(**self.urls),
            'index': '{base_url}/index.php'.format(**self.urls),
            'search': '{base_url}/browse.php'.format(**self.urls)
        })

        self.username = None
        self.password = None

        self.minseed = None
        self.minleech = None

        self.cache = TVCache(self, min_time=20)

    def _check_auth(self):
        if self.username and self.password:
            return True

        raise AuthException('Your authentication credentials for ' + self.name + ' are missing, check your config.')

    def _get_season_search_strings(self, ep_obj):

        search_string = {'Season': []}

        for show_name in set(show_names.allPossibleShowNames(ep_obj.show)):
            for sep in ' ', ' - ':
                season_string = show_name + sep + 'Series '
                if ep_obj.show.air_by_date or ep_obj.show.sports:
                    season_string += str(ep_obj.airdate).split('-')[0]
                elif ep_obj.show.anime:
                    season_string += '%d' % ep_obj.scene_absolute_number
                else:
                    season_string += '%d' % int(ep_obj.scene_season)

                search_string['Season'].append(re.sub(r'\s+', ' ', season_string.replace('.', ' ').strip()))

        return [search_string]

    def _get_episode_search_strings(self, ep_obj, add_string=''):

        search_string = {'Episode': []}

        if not ep_obj:
            return []

        for show_name in set(show_names.allPossibleShowNames(ep_obj.show)):
            for sep in ' ', ' - ':
                ep_string = sanitizeSceneName(show_name) + sep
                if ep_obj.show.air_by_date:
                    ep_string += str(ep_obj.airdate).replace('-', '|')
                elif ep_obj.show.sports:
                    ep_string += str(ep_obj.airdate).replace('-', '|') + '|' + ep_obj.airdate.strftime('%b')
                elif ep_obj.show.anime:
                    ep_string += '%i' % int(ep_obj.scene_absolute_number)
                else:
                    ep_string += sickrage.app.naming_ep_type[2] % {'seasonnumber': ep_obj.scene_season,
                                                                   'episodenumber': ep_obj.scene_episode}

                if add_string:
                    ep_string += ' %s' % add_string

                search_string['Episode'].append(re.sub(r'\s+', ' ', ep_string.replace('.', ' ').strip()))

        return [search_string]

    def login(self):
        if any(dict_from_cookiejar(self.session.cookies).values()):
            return True

        login_params = {'username': self.username, 'password': self.password}

        try:
            response = self.session.post(self.urls['login'], data=login_params, timeout=30).text
        except Exception:
            sickrage.app.log.warning("Unable to connect to provider")
            return False

        if re.search('Error: Username or password incorrect!', response):
            sickrage.app.log.warning(
                "Invalid username or password. Check your settings")
            return False

        return True

    def search(self, search_strings, age=0, ep_obj=None, **kwargs):
        results = []

        search_params = {
            'do': 'search',
            'keywords': '',
            'search_type': 't_name',
            'category': 0,
            'include_dead_torrents': 'no',
        }

        if not self.login():
            return results

        for mode in search_strings:
            sickrage.app.log.debug("Search Mode: %s" % mode)
            for search_string in search_strings[mode]:

                if mode != 'RSS':
                    sickrage.app.log.debug("Search string: %s " % search_string)

                search_params['keywords'] = search_string.strip()

                try:
                    data = self.session.get(self.urls['search'], params=search_params).text
                    results += self.parse(data, mode, keywords=search_string)
                except Exception:
                    sickrage.app.log.debug("No data returned from provider")
                    continue

        return results

    def parse(self, data, mode, **kwargs):
        """
        Parse search results from data
        :param data: response data
        :param mode: search mode
        :return: search results
        """

        results = []

        keywords = kwargs.pop('keywords', None)

        with bs4_parser(data) as html:
            torrent_table = html.find(id='sortabletable')
            torrent_rows = torrent_table('tr') if torrent_table else []

            if len(torrent_rows) < 2:
                sickrage.app.log.debug('Data returned from provider does not contain any torrents')
                return results

            labels = [label.img['title'] if label.img else label.get_text(strip=True) for label in
                      torrent_rows[0]('td')]

            for row in torrent_rows[1:]:
                try:
                    # Skip highlighted torrents
                    if mode == 'RSS' and row.get('class') == ['highlight']:
                        continue

                    title = row.find(class_='tooltip-content')
                    title = title.div.get_text(strip=True) if title else None
                    download_url = row.find(title='Click to Download this Torrent!')
                    download_url = download_url.parent['href'] if download_url else None
                    if not all([title, download_url]):
                        continue

                    seeders = try_int(row.find(title='Seeders').get_text(strip=True))
                    leechers = try_int(row.find(title='Leechers').get_text(strip=True))

                    # Chop off tracker/channel prefix or we cant parse the result!
                    if mode != 'RSS' and keywords:
                        show_name_first_word = re.search(r'^[^ .]+', keywords).group()
                        if not title.startswith(show_name_first_word):
                            title = re.sub(r'.*(' + show_name_first_word + '.*)', r'\1', title)

                    # Change title from Series to Season, or we can't parse
                    if mode == 'Season':
                        title = re.sub(r'(.*)(?i)Series', r'\1Season', title)

                    # Strip year from the end or we can't parse it!
                    title = re.sub(r'(.*)[. ]?\(\d{4}\)', r'\1', title)
                    title = re.sub(r'\s+', r' ', title)

                    torrent_size = row('td')[labels.index('Size')].get_text(strip=True)
                    size = convert_size(torrent_size, -1)

                    results += [
                        {'title': title, 'link': download_url, 'size': size, 'seeders': seeders,
                         'leechers': leechers}
                    ]

                    if mode != 'RSS':
                        sickrage.app.log.debug("Found result: {}".format(title))
                except Exception:
                    sickrage.app.log.error("Failed parsing provider.")

        return results
