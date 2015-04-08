# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2010-2015 Elico Corp (<http://www.elico-corp.com>)
#    Chen Rong <chen.rong@elico-corp.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
import base64
try:
    import simplejson as json
except ImportError:
    import json
from xml.etree import ElementTree
from collections import OrderedDict
import six
import urlparse
import logging
import requests
import time
try:
    from urllib import urlencode
except ImportError:
    from urllib.parse import urlencode
import werkzeug.urls
import urllib2
from hashlib import md5
from openerp.addons.payment.models.payment_acquirer import ValidationError
from openerp.addons.payment_alipay_wap.controllers.main import AlipayWapController
from openerp.osv import osv, fields
from openerp.tools.float_utils import float_compare
from openerp import SUPERUSER_ID

_logger = logging.getLogger(__name__)

class AcquirerAlipayWap(osv.Model):
    _inherit = 'payment.acquirer'

    def _get_alipay_wap_urls(self, cr, uid, environment, context=None):
        """ Alipay URLs
        """
        return {
            'alipay_wap_form_url': 'http://wappaygw.alipay.com/service/rest.htm',
        }

    def _get_providers(self, cr, uid, context=None):
        providers = super(AcquirerAlipayWap, self)._get_providers(cr, uid, context=context)
        providers.append(['alipay_wap', 'AlipayWap'])
        return providers

    _columns = {
        'alipay_wap_pid': fields.char('PID', required_if_provider='alipay_wap'),
        'alipay_wap_key': fields.char('Key', required_if_provider='alipay_wap'),
        'alipay_wap_seller_email': fields.char('Seller Email', required_if_provider='alipay_wap'),
    }

    def _alipay_wap_generate_md5_sign(self, acquirer, inout, values):
        """ Generate the md5sign for incoming or outgoing communications.

        :param browse acquirer: the payment.acquirer browse record. It should
                                have a md5key in shaky out
        :param string inout: 'in' (openerp contacting alipay) or 'out' (alipay
                             contacting openerp).
        :param dict values: transaction values

        :return string: md5sign
        """
        assert inout in ('in', 'out')
        assert acquirer.provider == 'alipay_wap'

        alipay_wap_key = acquirer.alipay_wap_key

        if inout == 'out':
            keys = ['request_token','result','out_trade_no','trade_no']
            src = '&'.join(['%s=%s' % (key, value) for key,
                        value in sorted(values.items()) if key in keys]) + alipay_wap_key
        else:
            keys = ['format','v','_input_charset','partner','service','req_data','sec_id']
            src = '&'.join(['%s=%s' % (key, value) for key,
                            value in sorted(values.items()) if key in keys])  + alipay_wap_key
        return md5(src.encode('utf-8')).hexdigest()

    def _alipay_wap_get_req_data(self, acquirer, alipay_wap_tx_values):

        keys =['notify_url','subject', 'out_trade_no', 'total_fee', 'seller_account_name','call_back_url']
        req_data = ''.join([alipay_wap_tx_values['_xmlnode'] % (key, value, key) for (key, value) in six.iteritems(alipay_wap_tx_values) if key in keys])
 
        req_data = alipay_wap_tx_values['_xmlnode'] % (
            alipay_wap_tx_values['TOKEN_ROOT_NODE'], req_data, alipay_wap_tx_values['TOKEN_ROOT_NODE'])
        params = {'req_data': req_data, 'req_id': time.time(), 'format': 'xml', 'v': '2.0', 
                  'partner': alipay_wap_tx_values['partner'], '_input_charset': 'utf-8',
                  }
        params['service'] = 'alipay.wap.trade.create.direct'

        signkey, signvalue = ('sec_id', 'MD5')
        params.update({signkey:signvalue}) 
        alipay_wap_key = acquirer.alipay_wap_key

        src = '&'.join(['%s=%s' % (key, value) for key,
                        value in sorted(params.items()) if key not in keys])  + alipay_wap_key
        sign = md5(src.encode('utf-8')).hexdigest()

        params.update({'sign': sign})

        def encode_dict(params):
            return {k: six.u(v).encode('utf-8')
                    if isinstance(v, str) else v.encode('utf-8')
                    if isinstance(v, six.string_types) else v
                    for k, v in six.iteritems(params)}

        url = '%s?%s' % ('http://wappaygw.alipay.com/service/rest.htm', urlencode(encode_dict(params)))

        alipayres = requests.post(
            url, headers={'connection': 'close'}).text
        params = urlparse.parse_qs(urlparse.urlparse(alipayres).path, keep_blank_values=True)
        if 'res_data' in params:
            tree = ElementTree.ElementTree(ElementTree.fromstring(urlparse.unquote(params['res_data'][0])))
            token = tree.find("request_token").text
        res = {'req_data': alipay_wap_tx_values['_xmlnode'] %
                  (alipay_wap_tx_values['AUTH_ROOT_NODE'],
                   (alipay_wap_tx_values['_xmlnode'] % ('request_token', token, 'request_token')),
                   alipay_wap_tx_values['AUTH_ROOT_NODE'])}
        return res['req_data']

    def alipay_wap_form_generate_values(self, cr, uid, id, partner_values, tx_values, context=None):
        base_url = self.pool['ir.config_parameter'].get_param(cr, SUPERUSER_ID, 'web.base.url')
        acquirer = self.browse(cr, uid, id, context=context)

        alipay_wap_tx_values = dict(tx_values)
        alipay_wap_tx_values.update({
            'TOKEN_ROOT_NODE': 'direct_trade_create_req',
            'AUTH_ROOT_NODE': 'auth_and_execute_req',
            '_xmlnode': '<%s>%s</%s>',
            'seller_account_name': acquirer.alipay_wap_seller_email,
            '_input_charset': 'utf-8',
            'partner': acquirer.alipay_wap_pid,
            'service': 'alipay.wap.auth.authAndExecute',
            'out_trade_no': tx_values['reference'],
            'total_fee': tx_values['amount'],
            'subject': tx_values['reference'],
            'v': '2.0',
            'format': 'xml',
            'sec_id': 'MD5',
            'call_back_url': '%s' % urlparse.urljoin(base_url, AlipayWapController._return_url),
            'notify_url': '%s' % urlparse.urljoin(base_url, AlipayWapController._notify_url),
        })
        alipay_wap_tx_values['req_data'] = self._alipay_wap_get_req_data(acquirer, alipay_wap_tx_values)
        alipay_wap_tx_values['sign'] = self._alipay_wap_generate_md5_sign(acquirer, 'in', alipay_wap_tx_values)
        return partner_values, alipay_wap_tx_values

    def alipay_wap_get_form_action_url(self, cr, uid, id, context=None):
        acquirer = self.browse(cr, uid, id, context=context)
        return self._get_alipay_wap_urls(cr, uid, acquirer.environment, context=context)['alipay_wap_form_url']

class TxAlipayWap(osv.Model):
    _inherit = 'payment.transaction'

    _columns = {
        'alipay_wap_txn_tradeno': fields.char('Transaction Trade Number'),
    }

#     # --------------------------------------------------
#     # FORM RELATED METHODS
#     # --------------------------------------------------

    def _alipay_wap_form_get_tx_from_data(self, cr, uid, data, context=None):
        """ Given a data dict coming from alipay, verify it and find the related
        transaction record. """
        reference = data.get('out_trade_no')
        if not reference:
            error_msg = 'Alipay: received data with missing reference (%s)' % reference
            _logger.error(error_msg)
            raise ValidationError(error_msg)
      
        tx_ids = self.pool['payment.transaction'].search(cr, uid, [('reference', '=', reference)], context=context)
        if not tx_ids or len(tx_ids) > 1:
            error_msg = 'Alipay: received data for reference %s' % (reference)
            if not tx_ids:
                error_msg += '; no order found'
            else:
                error_msg += '; multiple order found'
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        tx = self.pool['payment.transaction'].browse(cr, uid, tx_ids[0], context=context)

        sign_check = self.pool['payment.acquirer']._alipay_wap_generate_md5_sign(tx.acquirer_id, 'out', data)
        if sign_check != data.get('sign'):
            error_msg = 'alipay: invalid md5str, received %s, computed %s' % (data.get('sign'), sign_check)
            _logger.warning(error_msg)
            raise ValidationError(error_msg)

        return tx

    def _alipay_wap_form_get_invalid_parameters(self, cr, uid, tx, data, context=None):
        invalid_parameters = []

        if tx.acquirer_reference and data.get('out_trade_no') != tx.acquirer_reference:
            invalid_parameters.append(('Transaction Id', data.get('out_trade_no'), tx.acquirer_reference))


        return invalid_parameters

    def _alipay_wap_form_validate(self, cr, uid, tx, data, context=None):
        if data.get('result') in ['success']:
            tx.write({
                'state': 'done',
                'acquirer_reference': data.get('out_trade_no'),
                'alipay_wap_txn_tradeno': data.get('trade_no'),
            })
            return True
        else:
            error = 'Alipay: feedback error.'
            _logger.info(error)
            tx.write({
                'state': 'error',
                'state_message': error,
            })
            return False

