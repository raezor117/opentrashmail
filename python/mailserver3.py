import asyncio
from aiosmtpd.controller import Controller
from email.parser import BytesParser
from email import policy
import os
import re
import time
import json
import hashlib
import configparser
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging

logger = logging.getLogger(__name__)

# globals for settings
DISCARD_UNKNOWN = False
DELETE_OLDER_THAN_DAYS = False
DOMAINS = []
LAST_CLEANUP = 0
URL = ""

class CustomHandler:
    async def handle_DATA(self, server, session, envelope):
        peer = session.peer
        rcpts = []
        for rcpt in envelope.rcpt_tos:
            rcpts.append(rcpt)
        
        logger.debug('Receiving message from: %s:%d' % peer)
        logger.debug('Message addressed from: %s' % envelope.mail_from)
        logger.debug('Message addressed to: %s' % str(rcpts))

        filenamebase = str(int(round(time.time() * 1000)))

        # Get the raw email data
        raw_email = envelope.content.decode('utf-8')

        # Parse the email
        message = BytesParser(policy=policy.default).parsebytes(envelope.content)

        # Separate HTML and plaintext parts
        plaintext = ''
        html = ''
        attachments = {}
        for part in message.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get_content_type() == 'text/plain':
                #if it's a file
                if part.get_filename() is not None:
                    attachments['file%d' % len(attachments)] = self.handleAttachment(part)
                else:
                    plaintext += part.get_payload()
            elif part.get_content_type() == 'text/html':
                html += part.get_payload()
            else:
                attachments['file%d' % len(attachments)] = self.handleAttachment(part)

        for em in rcpts:
                em = em.lower()
                if not re.match(r"[^@\s]+@[^@\s]+\.[a-zA-Z0-9]+$", em):
                    logger.exception('Invalid recipient: %s' % em)
                    continue

                domain = em.split('@')[1]
                found = False
                for x in DOMAINS:
                    if  "*" in x and domain.endswith(x.replace('*', '')):
                        found = True
                    elif domain == x:
                        found = True
                if(DISCARD_UNKNOWN and found==False):
                    logger.info('Discarding email for unknown domain: %s' % domain)
                    continue

                if not os.path.exists("../data/"+em):
                    os.mkdir( "../data/"+em, 0o755 )


                edata = {
                    'subject': message['subject'],
                    'body': plaintext,
                    'htmlbody': self.replace_cid_with_attachment_id(html, attachments,filenamebase,em),
                    'from': message['from'],
                    'attachments':[],
                    'attachments_details':[]
                }
                savedata = {'sender_ip':peer[0],
                    'from':message['from'],
                    'rcpts':rcpts,
                    'raw':raw_email,
                    'parsed':edata
                }
                
                #same attachments if any
                for att in attachments:
                    if not os.path.exists("../data/"+em+"/attachments"):
                        os.mkdir( "../data/"+em+"/attachments", 0o755 )
                    attd = attachments[att]
                    file_id = attd[3]
                    file = open("../data/"+em+"/attachments/"+file_id, 'wb')
                    file.write(attd[1])
                    file.close()
                    edata["attachments"].append(file_id)
                    edata["attachments_details"].append({
                            "filename":attd[0],
                            "cid":attd[2],
                            "id":attd[3],
                            "download_url":URL+"/api/attachment/"+em+"/"+file_id,
                            "size":len(attd[1])
                        })

                # save actual json data
                with open("../data/"+em+"/"+filenamebase+".json", "w") as outfile:
                    json.dump(savedata, outfile)

        return '250 OK'

    def handleAttachment(self, part):
        filename = part.get_filename()
        if filename is None:
            filename = 'untitled'
        cid = part.get('Content-ID')
        if cid is not None:
            cid = cid[1:-1]
        elif part.get('X-Attachment-Id') is not None:
            cid = part.get('X-Attachment-Id')
        else: # else create a unique id using md5 of the attachment
            cid = hashlib.md5(part.get_payload(decode=True)).hexdigest()
        fid = hashlib.md5(filename.encode('utf-8')).hexdigest()+filename
        logger.debug('Handling attachment: "%s" (ID: "%s") of type "%s" with CID "%s"',filename, fid,part.get_content_type(), cid)

        return (filename,part.get_payload(decode=True),cid,fid)
    
    def replace_cid_with_attachment_id(self, html_content, attachments,filenamebase,email):
        # Replace cid references with attachment filename
        for attachment_id in attachments:
            attachment = attachments[attachment_id]
            filename = attachment[0]
            cid = attachment[2]
            if cid is None:
                continue
            cid = cid[1:-1]
            if cid is not None:
                html_content = html_content.replace('cid:' + cid, "/api/attachment/"+email+"/"+filenamebase+"-"+filename)
        return html_content

def cleanup():
    if(DELETE_OLDER_THAN_DAYS == False or time.time() - LAST_CLEANUP < 86400):
        return
    logger.info("Cleaning up")
    rootdir = '../data/'
    for subdir, dirs, files in os.walk(rootdir):
        for file in files:
            if(file.endswith(".json")):
                filepath = os.path.join(subdir, file)
                file_modified = os.path.getmtime(filepath)
                if(time.time() - file_modified > (DELETE_OLDER_THAN_DAYS * 86400)):
                    os.remove(filepath)
                    logger.info("Deleted file: " + filepath)

async def run(port):
    controller = Controller(CustomHandler(), hostname='0.0.0.0', port=port)
    controller.start()
    logger.info("[i] Ready to receive Emails")
    logger.info("")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        controller.stop()

if __name__ == '__main__':
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(ch)

    if not os.path.isfile("../config.ini"):
        logger.info("[ERR] Config.ini not found. Rename example.config.ini to config.ini. Defaulting to port 25")
        port = 25
    else:
        Config = configparser.ConfigParser(allow_no_value=True)
        Config.read("../config.ini")
        port = int(Config.get("MAILSERVER", "MAILPORT"))
        if("discard_unknown" in Config.options("MAILSERVER")):
            DISCARD_UNKNOWN = (Config.get("MAILSERVER", "DISCARD_UNKNOWN").lower() == "true")
        DOMAINS = Config.get("GENERAL", "DOMAINS").lower().split(",")
        URL = Config.get("GENERAL", "URL")

        if("CLEANUP" in Config.sections() and "delete_older_than_days" in Config.options("CLEANUP")):
            DELETE_OLDER_THAN_DAYS = (Config.get("CLEANUP", "DELETE_OLDER_THAN_DAYS").lower() == "true")

    logger.info("[i] Starting Mailserver on port " + str(port))
    logger.info("[i] Discard unknown domains: " + str(DISCARD_UNKNOWN))
    logger.info("[i] Listening for domains: " + str(DOMAINS))

    asyncio.run(run(port))
