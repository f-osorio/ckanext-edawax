# -*- coding: utf-8 -*-
# Hendrik Bunke
# ZBW - Leibniz Information Centre for Economics

from ckan.controllers.package import PackageController
import ckan.plugins.toolkit as tk
from ckan.common import c, request, _, response
from ckan import model
import ckan.lib.helpers as h
import logging
from ckan.authz import get_group_or_org_admin_ids
from ckanext.dara.helpers import check_journal_role
from functools import wraps
import notifications as n
from ckanext.edawax.helpers import is_private
from pylons import config

# for download all
import os
import time
import zipfile
import requests
import StringIO
from ckanext.dara.helpers import _parse_authors

from ckanext.edawax.helpers import is_reviewer
from ckanext.edawax.update import user_exists, update_maintainer_field

import ast
from webob import Response, Request

log = logging.getLogger(__name__)


def admin_req(func):
    @wraps(func)
    def check(*args, **kwargs):
        id = kwargs['id']
        controller = args[0]
        pkg = tk.get_action('package_show')(None, {'id': id})
        if not check_journal_role(pkg, 'admin') and not h.check_access('sysadmin'):
            return func(controller, id)
    return check


class WorkflowController(PackageController):
    """
    """

    def _context(self):
        return {'model': model, 'session': model.Session,
                'user': c.user or c.author, 'for_view': True,
                'auth_user_obj': c.userobj}

    def review(self, id):
        """
        sends review notification to all journal admins

        Check the maintainer's: if one is an email address, invite that person
        to the JDA as a reviewer - need a new invitation that includes a link
        to the dataset for review.
        """

        context = self._context()

        try:
            tk.check_access('package_update', context, {'id': id})
        except tk.NotAuthorized:
            tk.abort(403, 'Unauthorized')

        c.pkg_dict = tk.get_action('package_show')(context, {'id': id})

        # avoid multiple notifications (eg. when someone calls review directly)
        if c.pkg_dict.get('dara_edawax_review', 'false') == 'true':
            h.flash_error("Package has already been sent to review")
            redirect(id)

        user_name = tk.c.userobj.fullname or tk.c.userobj.email
        admins = get_group_or_org_admin_ids(c.pkg_dict['owner_org'])
        addresses = map(lambda admin_id: model.User.get(admin_id).email,admins)

        reviewer_names = []
        reviewer_names.append(c.pkg_dict['maintainer'])
        reviewer_names.append(c.pkg_dict['maintainer_email'])
        reviewer_emails = []
        # If either reviewer's name is an email address. Then create new user
        # and update the field to be their new name.
        for name in reviewer_names:
            if '@' in name:
                email = name
                user_exists = email_exists(email)
                data_dict = c.pkg_dict
                if user_exists:
                    data_dict = update_mainter_field(user_exists, data_dict)
                else:
                    try:
                        org_id = data_dict['organization']['id']
                    except KeyError:
                        org_id = data_dict['owner_org']
                new_user = invite_reviwer(email, org_id)
                data_dict = update_mainter_field(new_user['name'], data_dict)
            else:
                # otherwise just notify them that they can review
                try:
                    reviewer_emails.append(tk.get_action('user_show')(context, {'id': name})['email'])
                except Exception as e:
                    reviewer_emails.append(None)
        note = n.review(addresses, user_name, id, reviewer_emails)

        if note:
            c.pkg_dict['dara_edawax_review'] = 'true'
            tk.get_action('package_update')(context, c.pkg_dict)
            h.flash_success('Notification to Editors sent.')
        else:
            h.flash_error('ERROR: Mail could not be sent. Please try again later or contact the site admin.')

        redirect(id)

    @admin_req
    def publish(self, id):
        """
        publish dataset
        """
        context = self._context()
        c.pkg_dict = tk.get_action('package_show')(context, {'id': id})
        c.pkg_dict.update({'private': False, 'dara_edawax_review': 'reviewed'})
        tk.get_action('package_update')(context, c.pkg_dict)
        h.flash_success('Dataset published')
        redirect(id)

    @admin_req
    def retract(self, id):
        """
        set dataset private and back to review state
        """
        context = self._context()
        c.pkg_dict = tk.get_action('package_show')(context, {'id': id})

        if c.pkg_dict.get('dara_DOI_Test', False) and not h.check_access('sysadmin'):
            h.flash_error("ERROR: DOI (Test) already assigned, dataset can't be retracted")
            redirect(id)

        if c.pkg_dict.get('dara_DOI', False):
            h.flash_error("ERROR: DOI already assigned, dataset can't be retracted")
            redirect(id)

        c.pkg_dict.update({'private': True, 'dara_edawax_review': 'true'})
        tk.get_action('package_update')(context, c.pkg_dict)
        h.flash_success('Dataset retracted')
        redirect(id)

    @admin_req
    def reauthor(self, id):
        """reset dataset to private and leave review state.
        Should also send email to author
        """
        context = self._context()
        msg = tk.request.params.get('msg', '')
        c.pkg_dict = tk.get_action('package_show')(context, {'id': id})
        creator_mail = model.User.get(c.pkg_dict['creator_user_id']).email
        note = n.reauthor(id, creator_mail, msg, context)

        if note:
            c.pkg_dict.update({'private': True,
                               'dara_edawax_review': 'reauthor'})
            tk.get_action('package_update')(context, c.pkg_dict)
            h.flash_success('Notification sent. Dataset can now be re-edited by author')
        else:
            h.flash_error('ERROR: Mail could not be sent. Please try again later or contact the site admin.')
        redirect(id)


    def editor_notify(self, id):
        context = self._context()
        msg = tk.request.params.get('msg', '')
        c.pkg_dict = tk.get_action('package_show')(context, {'id': id})
        creator_mail = model.User.get(c.pkg_dict['creator_user_id']).email
        note = n.editor_notify(id, creator_mail, msg, context)

        if note:
            c.pkg_dict.update({'private': True, 'dara_edawax_review': ''})
            tk.get_action('package_update')(context, c.pkg_dict)
            h.flash_success('Notification sent. Journal editor will...')
        else:
            h.flash_error('ERROR: Mail could not be sent. Please try again later or contact the site admin.')
        redirect(id)


    def create_citataion_text(self, id):
        """ Create a plain text file with a citation. Will be included in
            the "download_all" zip file
         """
        context = self._context()
        data = tk.get_action('package_show')(context, {'id': id})
        citation = '{authors} ({year}): {dataset}. Version: {version}. {journal}. Dataset. {address}'

        journal_map = {'GER': 'German Economic Review', 'AEQ': 'Applied Economics Quarterly', 'IREE': 'International Journal for Re-Views in Empirical Economics', 'VSWG': 'Vierteljahrschrift für Sozial- und Wirtschaftsgeschichte'}

        authors = _parse_authors(data['dara_authors'])
        year = data.get('dara_PublicationDate', '')
        dataset_name = data.get('title', '').encode('utf-8')
        dataset_version = data.get('dara_currentVersion', '')

        temp_title = data['organization']['title']
        if temp_title in journal_map.keys():
            journal_title = journal_map[temp_title]
        else:
            journal_title = temp_title

        if data['dara_DOI'] != '':
            address = 'https://doi.org/{}'.format(data['dara_DOI'])
        else:
            address = '{}/dataset/{}'.format(config.get('ckan.site_url'), data['name'])

        return citation.format(authors=authors,
                               year=year,
                               dataset=dataset_name,
                               version=dataset_version,
                               journal=journal_title,
                               address=address)


    def download_all(self, id):
        data = {}
        context = self._context()
        c.pkg_dict = tk.get_action('package_show')(context, {'id': id})
        zip_sub_dir = 'resources'
        zip_name = u"{}_resouces_{}.zip".format(c.pkg_dict['title'].replace(' ', '_').replace(',', '_'), time.time())

        resources = c.pkg_dict['resources']
        for resource in resources:
            rsc = tk.get_action('resource_show')(context, {'id': resource['id']})
            if rsc.get('url_type') == 'upload':
                url = resource['url']
                filename = os.path.basename(url)
                # custom user agent header so that downloads from here count
                headers = {
                    'User-Agent': 'Ckan-Download-All Agent 1.0',
                    'From': 'f.osorio@zbw.eu'
                }
                r = requests.get(url, stream=True, headers=headers)
                if r.status_code != 200:
                    h.flash_error('Failed to download files.')
                    redirect(id)
                else:
                    data[filename] = r

        data['citation.txt'] = self.create_citataion_text(id)
        if len(data) > 0:
            s = StringIO.StringIO()
            zf = zipfile.ZipFile(s, "w")
            for item, content in data.items():
                zip_path = os.path.join(zip_sub_dir, item)
                try:
                    zf.writestr(zip_path, content.content)
                except Exception as e:
                    # adding the citation file
                    zf.writestr(zip_path, content)
            zf.close()
            response.headers.update({"Content-Disposition": "attachment;filename={}".format(zip_name.encode('utf8'))})
            response.content_type = "application/zip"
            return s.getvalue()
        # if there's nothing to download but someone gets to the download page
        # /download_all, return them to the landing page
        h.flash_error('Nothing to download.')
        redirect(id)





def redirect(id):
    tk.redirect_to(controller='package', action='read', id=id)

context = {'model': model, 'session': model.Session,
           'user': c.user or c.author, 'for_view': True,
           'auth_user_obj': c.userobj, 'ignore_auth': True}


def parse_ris_authors(authors):
    out = ''
    line = 'AU  - {last}, {first}\n'
    authors = ast.literal_eval(authors.replace("null", "None"))
    for author in authors:
        out += line.format(last=author['lastname'], first=author['firstname'])
    return out


def parse_bibtex_authors(authors):
    temp_str = ''
    temp_list = []
    authors = ast.literal_eval(authors.replace("null", "None"))
    for author in authors:
        temp_list.append('{}, {}'.format(author['lastname'], author['firstname']))
    if len(temp_list) > 1:
        return " and ".join(temp_list)
    else:
        return temp_list[0]



def parse_ris_doi(doi):
    if doi != '':
        return 'DO  - doi:{}\n'.format(doi)
    return ''

def create_ris_record(id):
    contents = "TY  - DATA\nT1  - {title}\n{authors}{doi}{abstract}{jels}ET  - {version}\nPY  - {date}\nPB  - ZBW - Leibniz Informationszentrum Wirtschaft\nUR  - {url}\nER  - \n"
    pkg_dict = tk.get_action('package_show')(context, {'id': id})
    title = pkg_dict['title'].encode('utf-8')
    try:
        authors = parse_ris_authors(pkg_dict['dara_authors'])
    except KeyError:
        if 'dara_authors' not in pkg_dict.keys():
            authors = pkg_dict['author'] or ''
        authors = ''
    date = pkg_dict['dara_PublicationDate']
    try:
        journal = pkg_dict['organization']['title']
    except TypeError as e:
        journal = ''
    url = '{}/dataset/{}'.format(config.get('ckan.site_url'), pkg_dict['name'])
    version = pkg_dict['dara_currentVersion']
    doi = parse_ris_doi(pkg_dict['dara_DOI'])

    if pkg_dict['notes'] != '':
        abstract = 'AB  - {}\n'.format(pkg_dict['notes'].encode('utf-8').replace('\n', ' ').replace('\r', ' '))
    else:
        abstract = ''

    if 'dara_jels' in pkg_dict.keys():
        jels = ''
        for jel in pkg_dict['dara_jels']:
            jels += 'KW  - {}\n'.format(jel)
    else:
        jels = ''

    contents = contents.format(title=title,authors=authors,doi=doi,date=date,journal=journal,url=url,version=version,abstract=abstract,jels=jels)

    s = StringIO.StringIO()
    s.write(contents)

    response.headers.update({"Content-Disposition": "attachment;filename={}_citation.ris".format(pkg_dict['name'])})
    response.content_type = "application/download"
    res = Response(content_type = "application/download")
    response.body = contents

    return res


def create_bibtex_record(id):
    pkg_dict = tk.get_action('package_show')(context, {'id': id})
    title = pkg_dict['title'].encode('utf-8')
    try:
        authors = parse_bibtex_authors(pkg_dict['dara_authors'])
    except KeyError:
        if 'dara_authors' not in pkg_dict.keys():
            authors = pkg_dict['author'] or ''
        authors = ''
    date = pkg_dict['dara_PublicationDate']
    try:
        journal = pkg_dict['organization']['title'].encode('utf-8')
    except TypeError as e:
        journal = ''
    url = '{}/dataset/{}'.format(config.get('ckan.site_url'), pkg_dict['name'])
    version = pkg_dict['dara_currentVersion']
    if pkg_dict['dara_DOI'] != '':
        temp_doi = pkg_dict['dara_DOI']
        identifier = '{}'.format(temp_doi.split('/')[1])
    else:
        identifier = '{}/{}'.format(pkg_dict['name'][:10], date)

    if pkg_dict['dara_DOI'] != '':
        doi = ',\ndoi = "{}"'.format(pkg_dict['dara_DOI'])
    else:
        doi = ''

    if 'dara_jels' in pkg_dict.keys():
        jels = ',\nkeywords = {'
        for x, jel in enumerate(pkg_dict['dara_jels']):
            if x < len(pkg_dict['dara_jels']) - 1:
                jels += '{},'.format(jel)
            else:
                jels += '{}}}'.format(jel)
    else:
        jels = ''

    contents = '@data{{{identifier},\nauthor = {{{authors}}},\npublisher = {{ZBW - Leibniz Informationszentrum Wirtschaft}},\ntitle = {{{title}}},\nyear = {{{date}}},\nversion = {{{version}}},\nurl = {{{url}}}{jels}{doi} \n}}'

    contents = contents.format(identifier=identifier, authors=authors, title=title,date=date,version=version,url=url,doi=doi,jels=jels)

    s = StringIO.StringIO()
    s.write(contents)

    response.headers.update({"Content-Disposition": "attachment;filename={}_citation.bib".format(pkg_dict['name'])})
    response.content_type = "text/plain"
    res = Response(content_type = "application/download")
    response.body = contents

    return res



class InfoController(tk.BaseController):

    TEMPLATE = "info_index.html"

    def index(self):
        return tk.render(self.TEMPLATE, extra_vars={'page': 'index'})

    def md_page(self):
        plist = tk.request.path.rsplit('/', 1)
        return tk.render(self.TEMPLATE, extra_vars={'page': plist[-1]})


    def create_citation(self, type, id):
        if type == 'ris':
            create_ris_record(id)
        elif type == 'bibtex':
            create_bibtex_record(id)
        else:
            h.flash_error("Couldn't build {} citation.".format(type))
            redirect(id)
