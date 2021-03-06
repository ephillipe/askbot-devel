"""authentication backend that takes care of the
multiple login methods supported by the authenticator
application
"""
import datetime
import logging
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings as django_settings
from django.utils.translation import ugettext as _
from askbot.deps.django_authopenid.models import UserAssociation
from askbot.deps.django_authopenid import util
from askbot.conf import settings as askbot_settings
from askbot.models.signals import user_registered

log = logging.getLogger('configuration')

def split_name(full_name, name_format):
    bits = full_name.strip().split()
    if len(bits) == 1:
        bits.push('')
    elif len(bits) == 0:
        bits = ['', '']

    if name_format == 'first,last':
        return bits[0], bits[1]
    elif name_format == 'last,first':
        return bits[1], bits[0]
    else:
        raise ValueError('Unexpected value of name_format')


def ldap_authenticate(username, password):
    """
    Authenticate using ldap
    
    python-ldap must be installed
    http://pypi.python.org/pypi/python-ldap/2.4.6
    """
    import ldap
    user_information = None
    try:
        ldap_session = ldap.initialize(askbot_settings.LDAP_URL)

        #set protocol version
        if askbot_settings.LDAP_PROTOCOL_VERSION == '2':
            ldap_session.protocol_version = ldap.VERSION2
        elif askbot_settings.LDAP_PROTOCOL_VERSION == '3':
            ldap_session.protocol_version = ldap.VERSION3
        else:
            raise NotImplementedError('unsupported version of ldap protocol')

        ldap.set_option(ldap.OPT_REFERRALS, 0)

        #set extra ldap options, if given
        if hasattr(django_settings, 'LDAP_EXTRA_OPTIONS'):
            options = django_settings.LDAP_EXTRA_OPTIONS
            for key, value in options:
                if key.startswith('OPT_'):
                    ldap_key = getattr(ldap, key)
                    ldap.set_option(ldap_key, value)
                else:
                    raise ValueError('Invalid LDAP option %s' % key)

        #add optional "master" LDAP authentication, if required
        master_username = getattr(django_settings, 'LDAP_USER', None)
        master_password = getattr(django_settings, 'LDAP_PASSWORD', None)

        login_name_field = askbot_settings.LDAP_LOGIN_NAME_FIELD
        base_dn = askbot_settings.LDAP_BASE_DN
        login_template = login_name_field + '=%s,' + base_dn
        encoding = askbot_settings.LDAP_ENCODING

        if master_username and master_password:
            login_dn = login_template % master_username
            ldap_session.simple_bind_s(
                login_dn.encode(encoding),
                master_password.encode(encoding)
            )

        user_filter = askbot_settings.LDAP_USER_FILTER_TEMPLATE % (
                        askbot_settings.LDAP_LOGIN_NAME_FIELD,
                        username
                    )

        email_field = askbot_settings.LDAP_EMAIL_FIELD

        get_attrs = [
            email_field.encode(encoding),
            login_name_field.encode(encoding)
            #str(askbot_settings.LDAP_USERID_FIELD)
            #todo: here we have a chance to get more data from LDAP
            #maybe a point for some plugin
        ]

        common_name_field = askbot_settings.LDAP_COMMON_NAME_FIELD.strip()
        given_name_field = askbot_settings.LDAP_GIVEN_NAME_FIELD.strip()
        surname_field = askbot_settings.LDAP_SURNAME_FIELD.strip()

        if given_name_field and surname_field:
            get_attrs.append(given_name_field.encode(encoding))
            get_attrs.append(surname_field.encode(encoding))
        elif common_name_field:
            get_attrs.append(common_name_field.encode(encoding))

        # search ldap directory for user
        user_search_result = ldap_session.search_s(
            askbot_settings.LDAP_BASE_DN.encode(encoding),
            ldap.SCOPE_SUBTREE,
            user_filter.encode(encoding),
            get_attrs 
        )
        if user_search_result: # User found in LDAP Directory
            user_dn = user_search_result[0][0]
            user_information = user_search_result[0][1]
            ldap_session.simple_bind_s(user_dn, password.encode(encoding)) #raises INVALID_CREDENTIALS
            ldap_session.unbind_s()
            
            exact_username = user_information[login_name_field][0]
            email = user_information[email_field][0]

            if given_name_field and surname_field:
                last_name = user_information[surname_field][0]
                first_name = user_information[given_name_field][0]
            elif surname_field:
                common_name_format = askbot_settings.LDAP_COMMON_NAME_FIELD_FORMAT
                common_name = user_information[common_name_field][0]
                first_name, last_name = split_name(common_name, common_name_format)
            
            #here we have an opportunity to copy password in the auth_user table
            #but we don't do it for security reasons
            try:
                user = User.objects.get(username__exact=exact_username)
                # always update user profile to synchronize with ldap server
                user.set_unusable_password()
                #user.first_name = first_name
                #user.last_name = last_name
                user.email = email
                user.save()
            except User.DoesNotExist:
                # create new user in local db
                user = User()
                user.username = exact_username
                user.set_unusable_password()
                #user.first_name = first_name
                #user.last_name = last_name
                user.email = email
                user.is_staff = False
                user.is_superuser = False
                user.is_active = True
                user.save()
                user_registered.send(None, user = user)

                log.info('Created New User : [{0}]'.format(exact_username))
            return user
        else:
            # Maybe a user created internally (django admin user)
            try:
                user = User.objects.get(username__exact=username)
                if user.check_password(password):
                    return user
                else:
                    return None
            except User.DoesNotExist:
                return None 

    except ldap.INVALID_CREDENTIALS, e:
        return None # Will fail login on return of None
    except ldap.LDAPError, e:
        log.error("LDAPError Exception")
        log.exception(e)
        return None
    except Exception, e:
        log.error("Unexpected Exception Occurred")
        log.exception(e)
        return None


class AuthBackend(object):
    """Authenticator's authentication backend class
    for more info, see django doc page:
    http://docs.djangoproject.com/en/dev/topics/auth/#writing-an-authentication-backend

    the reason there is only one class - for simplicity of
    adding this application to a django project - users only need
    to extend the AUTHENTICATION_BACKENDS with a single line
    """

    def authenticate(
                self,
                username = None,#for 'password' and 'ldap'
                password = None,#for 'password' and 'ldap'
                user_id = None,#for 'force'
                provider_name = None,#required with all except email_key
                openid_url = None,
                email_key = None,
                oauth_user_id = None,#used with oauth
                facebook_user_id = None,#user with facebook
                wordpress_url = None, # required for self hosted wordpress
                wp_user_id = None, # required for self hosted wordpress
                method = None,#requried parameter
            ):
        """this authentication function supports many login methods
        just which method it is going to use it determined
        from the signature of the function call
        """
        login_providers = util.get_enabled_login_providers()
        assoc = None # UserAssociation not needed for ldap
        if method == 'password':
            if login_providers[provider_name]['type'] != 'password':
                raise ImproperlyConfigured('login provider must use password')
            if provider_name == 'local':
                try:
                    user = User.objects.get(username=username)
                    if not user.check_password(password):
                        return None
                except User.DoesNotExist:
                    try:
                        email_address = username
                        user = User.objects.get(email = email_address)
                        if not user.check_password(password):
                            return None
                    except User.DoesNotExist:
                        return None
                    except User.MultipleObjectsReturned:
                        logging.critical(
                            ('have more than one user with email %s ' +
                            'he/she will not be able to authenticate with ' +
                            'the email address in the place of user name') % email_address
                        )
                        return None
            else:
                if login_providers[provider_name]['check_password'](username, password):
                    try:
                        #if have user associated with this username and provider,
                        #return the user
                        assoc = UserAssociation.objects.get(
                                        openid_url = username + '@' + provider_name,#a hack - par name is bad
                                        provider_name = provider_name
                                    )
                        return assoc.user
                    except UserAssociation.DoesNotExist:
                        #race condition here a user with this name may exist
                        user, created = User.objects.get_or_create(username = username)
                        if created:
                            user.set_password(password)
                            user.save()
                            user_registered.send(None, user = user)
                        else:
                            #have username collision - so make up a more unique user name
                            #bug: - if user already exists with the new username - we are in trouble
                            new_username = '%s@%s' % (username, provider_name)
                            user = User.objects.create_user(new_username, '', password)
                            user_registered.send(None, user = user)
                            message = _(
                                'Welcome! Please set email address (important!) in your '
                                'profile and adjust screen name, if necessary.'
                            )
                            user.message_set.create(message = message)
                else:
                    return None

            #this is a catch - make login token a little more unique
            #for the cases when passwords are the same for two users
            #from the same provider
            try:
                assoc = UserAssociation.objects.get(
                                            user = user,
                                            provider_name = provider_name
                                        )
            except UserAssociation.DoesNotExist:
                assoc = UserAssociation(
                                    user = user,
                                    provider_name = provider_name
                                )
            assoc.openid_url = username + '@' + provider_name#has to be this way for external pw logins

        elif method == 'openid':
            provider_name = util.get_provider_name(openid_url)
            try:
                assoc = UserAssociation.objects.get(
                                            openid_url = openid_url,
                                            provider_name = provider_name
                                        )
                user = assoc.user
            except UserAssociation.DoesNotExist:
                return None

        elif method == 'email':
            #with this method we do no use user association
            try:
                #todo: add email_key_timestamp field
                #and check key age
                user = User.objects.get(email_key = email_key)
                user.email_key = None #one time key so delete it
                user.email_isvalid = True
                user.save()
                return user
            except User.DoesNotExist:
                return None

        elif method == 'oauth':
            if login_providers[provider_name]['type'] == 'oauth':
                try:
                    assoc = UserAssociation.objects.get(
                                                openid_url = oauth_user_id,
                                                provider_name = provider_name
                                            )
                    user = assoc.user
                except UserAssociation.DoesNotExist:
                    return None
            else:
                return None

        elif method == 'facebook':
            try:
                #assert(provider_name == 'facebook')
                assoc = UserAssociation.objects.get(
                                            openid_url = facebook_user_id,
                                            provider_name = 'facebook'
                                        )
                user = assoc.user
            except UserAssociation.DoesNotExist:
                return None

        elif method == 'ldap':
            user = ldap_authenticate(username, password)

        elif method == 'wordpress_site':
            try:
                custom_wp_openid_url = '%s?user_id=%s' % (wordpress_url, wp_user_id)
                assoc = UserAssociation.objects.get(
                                            openid_url = custom_wp_openid_url,
                                            provider_name = 'wordpress_site'
                                            )
                user = assoc.user
            except UserAssociation.DoesNotExist:
                return None
        elif method == 'force':
            return self.get_user(user_id)
        else:
            raise TypeError('only openid and password supported')

        if assoc:
            #update last used time
            assoc.last_used_timestamp = datetime.datetime.now()
            assoc.save()
        return user

    def get_user(self, user_id):
        try:
            return User.objects.get(id=user_id)
        except User.DoesNotExist:
            return None

    @classmethod
    def set_password(cls, 
                    user=None,
                    password=None,
                    provider_name=None
                ):
        """generic method to change password of
        any for any login provider that uses password
        and allows the password change function
        """
        login_providers = util.get_enabled_login_providers()
        if login_providers[provider_name]['type'] != 'password':
            raise ImproperlyConfigured('login provider must use password')

        if provider_name == 'local':
            user.set_password(password)
            user.save()
            scrambled_password = user.password + str(user.id)
        else:
            raise NotImplementedError('external passwords not supported')

        try:
            assoc = UserAssociation.objects.get(
                                        user = user,
                                        provider_name = provider_name
                                    )
        except UserAssociation.DoesNotExist:
            assoc = UserAssociation(
                        user = user,
                        provider_name = provider_name
                    )

        assoc.openid_url = scrambled_password
        assoc.last_used_timestamp = datetime.datetime.now()
        assoc.save()
