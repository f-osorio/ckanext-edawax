import ast
from ckan import model
from ckan.common import g
import ckan.lib.helpers as h
import ckan.plugins.toolkit as tk


"""
START INFO Views
"""
TEMPLATE = "info_index.html"

def index():
    return tk.render(TEMPLATE, extra_vars={'page': 'index'})

def md_page(id):
    plist = tk.request.path.rsplit('/', 1)
    return tk.render(TEMPLATE, extra_vars={'page': plist[-1]})

def create_citation(type, id):
    if type == 'ris':
        create_ris_record(id)
    elif type == 'bibtex':
        create_bibtex_record(id)
    else:
        h.flash_error("Couldn't build {} citation.".format(type))
        redirect(id)

"""
END INFO Views
"""

def context():
    return {'model': model, 'session': model.Session,
           'user': g.user or g.author, 'for_view': True,
           'auth_user_obj': g.userobj, 'ignore_auth': True}

def redirect(id):
    tk.redirect_to(controller='package', action='read', id=id)

def create_citation(type, id):
    if type == 'ris':
        create_ris_record(id)
    elif type == 'bibtex':
        create_bibtex_record(id)
    else:
        h.flash_error("Couldn't build {} citation.".format(type))
        redirect(id)

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
    if 'dara_DOI' in pkg_dict.keys():
        doi = parse_ris_doi(pkg_dict['dara_DOI'])
    else:
        doi = ''

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
    if 'dara_DOI' in pkg_dict.keys() and pkg_dict['dara_DOI'] != '':
        temp_doi = pkg_dict['dara_DOI']
        identifier = '{}'.format(temp_doi.split('/')[1])
    else:
        identifier = '{}/{}'.format(pkg_dict['name'][:10], date)

    if 'dara_DOI' in pkg_dict.keys() and pkg_dict['dara_DOI'] != '':
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
