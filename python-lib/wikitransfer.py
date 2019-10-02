# -*- coding: utf-8 -*-

from md2conf import convert_info_macros, convert_comment_block, convert_code_block, add_images, process_refs, upload_attachment, urlEncodeNonAscii
import markdown, re, urllib, logging
from emoji import replace_emoji
from dataikuapi.dss.wiki import DSSWiki

from wikilinks import WikiLinkExtension
from attachmenttable import AttachmentTable

import os
import sys
reload(sys)
sys.setdefaultencoding('utf8')
import locale
os.environ["PYTHONIOENCODING"] = "utf-8"

from urllib   import quote

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format='confluence plugin %(levelname)s - %(message)s')

class WikiTransfer(AttachmentTable):
    attachment_table = AttachmentTable()
    transfered_attachments = []
    LINK_RE = ur'[\u0080\u0081\u0099\u00a9\u00ae\u2000-\u3300\ud83c\ud000-\udfff\ud83d\ud000-\udfff\ud83e\ud000-\udfff\w\X0-9_ \-\.&\(\)]+'
    LINK_URL_RE = ur'[\u0080\u0081\u0099\u00a9\u00ae\u2000-\u3300\ud83c\ud000-\udfff\ud83d\ud000-\udfff\ud83e\ud000-\udfff\w\X0-9_&;:\|\' \-\.\(\)]+'

    def recurse_taxonomy(self, taxonomy, ancestor = None):
        for article in taxonomy:
            if len(article['children']) > 0:
                confluence_id = self.transfer_article(article['id'], ancestor)
                self.recurse_taxonomy(article['children'], confluence_id)
            else:
                confluence_id = self.transfer_article(article['id'], ancestor)

    def transfer_article(self, article_id, parent_id = None):
        self.attachment_table.reset()
        self.transfered_attachments = []
        article_data = self.wiki.get_article(article_id).get_data()
        dss_page_name = article_data.get_name()
        dss_page_body = article_data.get_body()

        try:
            status = self.confluence.get_page_by_title(self.confluence_space_key, dss_page_name)
        except:
            # Unhandled exception in get_page_by_title when space key is not found. 
            # If we reached this stage, it means the space key exists, so the casing is probably off.
            raise Exception("The Confluence space key \"" + self.confluence_space_key + "\" overlaps with an existing one. Please check its casing.")

        if status is None or "id" not in status:
            status = self.confluence.create_page(
                space=self.confluence_space_key,
                title=dss_page_name,
                body="",
                parent_id = parent_id
            )
            self.check_status(status, "creating the Confluence page")

        new_id = status['id']

        confluence_page_body = self.convert(dss_page_body, article_id, new_id, article_data)

        status = self.confluence.update_page(
            page_id = new_id,
            title = dss_page_name,
            body=confluence_page_body
        )

        if self.has_error_status_code(status):
            logger.error("Could not upload page \"" + dss_page_name + '"')
            error_message = "Could not upload this page from DSS. "
            if "message" in status:
                error_message = error_message + 'The error message was: ' + status["message"]
            self.confluence.update_page(
                page_id = new_id,
                title = dss_page_name,
                body = error_message
            )

        self.update_progress()
        return new_id

    def convert(self, md_input, article_id, new_id, article_data):
        md = replace_emoji(md_input)
        md = self.process_attached_images(md, article_id, new_id)

        if len(article_data.article_data['article']['attachments']) > 0:
            self.process_attachments(new_id, article_data)

        md = md + u'\n' + self.attachment_table.to_md()
        md = self.develop_dss_links(md)

        html = markdown.markdown(md, extensions=['markdown.extensions.tables',
                                                       'markdown.extensions.fenced_code',
                                                       'markdown.extensions.nl2br',
                                                       'markdown.extensions.extra',
                                                       WikiLinkExtension()])

        html = self.convert_dss_refs_in_wikilinks(html)
        html = convert_info_macros(html)
        html = convert_comment_block(html)
        html = convert_code_block(html)
        html = process_refs(html)

        html = add_images(
            new_id,
            self.studio_external_url,
            self.confluence_url,
            html,
            self.confluence_username,
            self.confluence_password
        )

        return html

    def convert_dss_refs_in_wikilinks(self, html):
        return re.sub(
            ur'content-title="('+ self.project_key +ur')\.(' + self.LINK_URL_RE + ur')">',ur'content-title="\2">',
            html,
            flags=re.IGNORECASE
        )

    def develop_dss_links(self, md):
        links = self.find_dss_links(md)
        for link in links:
            object_type = link[0]
            project_key = self.project_key if link[2] == '' and link[0].lower() != 'project' else link[1]
            object_id = link[1] if link[2] == '' else link[2]
            initial_id = object_id if link[2] == '' else project_key + '.' + object_id
            object_path = self.build_dss_path(object_type, project_key, object_id)

            md = re.sub(r'\(' + object_type + r':' + initial_id + r'\)', '(' + object_path + ')',  md, flags=re.IGNORECASE)
            md = re.sub( object_type + r':' + initial_id, self.build_dss_url(object_type, object_path),  md, flags=re.IGNORECASE)
        return md

    def find_dss_links(self, md):
        dss_links_regexp = re.compile(r'(\bsaved_model\b|\binsight\b|\bproject\b|\bdataset\b):([a-zA-Z0-9_]+)\.?([a-zA-Z0-9_]+)?',flags=re.I | re.X)
        return dss_links_regexp.findall(md)

    def build_dss_path(self, object_type, project_key, object_id):
        path_type = {
            'saved_model': '/savedmodels/' + object_id + '/versions/',
            'insight': '/dashboards/insights/' + object_id + '_/view',
            'project': '/',
            'dataset': '/datasets/'+ object_id + '/explore/'
        }
        return self.studio_external_url + '/projects/' + project_key + path_type[object_type.lower()]

    def build_dss_url(self, object_type, object_path):
        return '<a href="' + object_path + '">' + object_type + '</a>'

    def process_attached_images(self, md, article_id, new_id):
        links = re.findall(
            ur'\[(' + self.LINK_RE + ur')\]\(([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\)',
            md.decode('utf-8'),
            flags=re.UNICODE
        )
        links = self.remove_duplicate_links(links)
        for link in links:
            if link[0] in self.transfered_attachments:
                continue
            article = self.wiki.get_article(article_id)
            try:
                image = self.get_uploaded_file(article, link[1], link[2])
                if link[0] == "":
                    file_name = link[1] + '.' + link[2]
                else:
                    file_name = link[0]
                if file_name not in self.transfered_attachments:
                    upload_attachment(new_id, file_name, "Uploaded by process_attached_images", self.confluence_url, self.confluence_username, self.confluence_password, raw = image)
                    self.transfered_attachments.append(file_name)
                print('ALX:sub with {0}/{1}'.format(link[1], link[2]))
                md = re.sub(
                    ur'!?\[' + self.LINK_RE + ur'\]\(' + link[1] + ur'\.' + link[2] + ur'\)',
                    '<ac:image ac:thumbnail="true"><ri:attachment ri:filename="'+ urlEncodeNonAscii(file_name) +'" /></ac:image>',
                    md.decode('utf-8'),
                    flags=re.UNICODE
                )
            except:
                md = re.sub(
                    ur'!?\[' + self.LINK_RE + ur'\]\(' + link[1] + ur'\.' + link[2] + ur'\)',
                    '*Image could not be transfered*',
                    md.decode('utf-8'),
                    flags=re.UNICODE
                )
        return md

    def get_uploaded_file(self, article, project_key, upload_id):
        if project_key == self.project_key:
            return article.get_uploaded_file(project_key, upload_id)
        else:
            wiki = DSSWiki(self.client, project_key)
            list_articles = wiki.list_articles()
            return list_articles[0].get_uploaded_file(project_key, upload_id)

    def remove_duplicate_links(self, links):
        # todo
        return links

    def format_confluence_url(self, server_type, server_name, organization_name):
        if server_type == "local":
            return server_name
        else:
            assert re.match('^[a-zA-Z0-9]+$', organization_name)
            return "https://" + organization_name + ".atlassian.net/wiki"

    def update_progress(self):
        self.progress = self.progress + 1
        self.progress_callback(self.progress)

    def process_attachments(self, article_id, article):
        for attachment in article.article_data['article']['attachments']:
            if attachment[u'attachmentType'] == 'FILE' and attachment[u'attachmentType'] not in self.transfered_attachments:
                attachment_name = attachment['details']['objectDisplayName']
                article = self.wiki.get_article(article.article_id)
                try:
                    file = article.get_uploaded_file(attachment_name, attachment['smartId'])
                    upload_attachment(article_id, attachment_name, "Uploaded by process_attachments", self.confluence_url, self.confluence_username, self.confluence_password, raw = file)
                    self.transfered_attachments.append(attachment_name)
                except Exception as err:
                    # get_uploaded_file not implemented yet on backend, older version of DSS
                    pass
            elif attachment[u'attachmentType'] == 'DSS_OBJECT':
                self.attachment_table.add(attachment)

    def check_space_key_format(self):
        if len(re.findall(r'[a-zA-Z0-9]', self.confluence_space_key)) != len(self.confluence_space_key):
            raise Exception('\nThe Confluence Space key contains illegal characters')

    def check_status(self, status, context):
        if status is None:
            return
        if "statusCode" in status:
            error_code = status["statusCode"]
        else:
            return
        if error_code / 100 != 2:
            raise Exception('Error '+ str(error_code) + ' while ' + context + ' : ' + status["message"] + ". " + self.get_advice(error_code))

    def get_advice(self, error_code):
        advice = {
            403: "Please check your rights on the target Confluence space"
        }
        if error_code in advice:
            return advice[error_code]
        else:
            return ""

    def has_error_status_code(self, status):
        if "statusCode" in status:
            error_code = status["statusCode"]
            if error_code / 100 != 2:
                return True
        return False