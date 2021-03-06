"""
:synopsis: most ajax processors for askbot

This module contains most (but not all) processors for Ajax requests.
Not so clear if this subdivision was necessary as separation of Ajax and non-ajax views
is not always very clean.
"""
import datetime
import logging
from django.conf import settings as django_settings
from django.core import exceptions
#from django.core.management import call_command
from django.core.urlresolvers import reverse
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect, Http404, HttpResponseBadRequest
from django.forms import ValidationError, IntegerField, CharField
from django.shortcuts import get_object_or_404
from django.views.decorators import csrf
from django.utils import simplejson
from django.utils.html import escape
from django.utils.translation import ugettext as _
from django.utils.translation import string_concat
from askbot import models
from askbot import forms
from askbot.conf import should_show_sort_by_relevance
from askbot.conf import settings as askbot_settings
from askbot.utils import category_tree
from askbot.utils import decorators
from askbot.utils import url_utils
from askbot import mail
from askbot.skins.loaders import render_into_skin, get_template
from askbot import const


@csrf.csrf_exempt
def manage_inbox(request):
    """delete, mark as new or seen user's
    response memo objects, excluding flags
    request data is memo_list  - list of integer id's of the ActivityAuditStatus items
    and action_type - string - one of delete|mark_new|mark_seen
    """

    response_data = dict()
    try:
        if request.is_ajax():
            if request.method == 'POST':
                post_data = simplejson.loads(request.raw_post_data)
                if request.user.is_authenticated():
                    activity_types = const.RESPONSE_ACTIVITY_TYPES_FOR_DISPLAY
                    activity_types += (
                        const.TYPE_ACTIVITY_MENTION,
                        const.TYPE_ACTIVITY_MARK_OFFENSIVE,
                        const.TYPE_ACTIVITY_MODERATED_NEW_POST,
                        const.TYPE_ACTIVITY_MODERATED_POST_EDIT
                    )
                    user = request.user
                    memo_set = models.ActivityAuditStatus.objects.filter(
                        id__in = post_data['memo_list'],
                        activity__activity_type__in = activity_types,
                        user = user
                    )

                    action_type = post_data['action_type']
                    if action_type == 'delete':
                        memo_set.delete()
                    elif action_type == 'mark_new':
                        memo_set.update(status = models.ActivityAuditStatus.STATUS_NEW)
                    elif action_type == 'mark_seen':
                        memo_set.update(status = models.ActivityAuditStatus.STATUS_SEEN)
                    elif action_type == 'remove_flag':
                        for memo in memo_set:
                            activity_type = memo.activity.activity_type
                            if activity_type == const.TYPE_ACTIVITY_MARK_OFFENSIVE:
                                request.user.flag_post(
                                    post = memo.activity.content_object,
                                    cancel_all = True
                                )
                            elif activity_type in \
                                (
                                    const.TYPE_ACTIVITY_MODERATED_NEW_POST,
                                    const.TYPE_ACTIVITY_MODERATED_POST_EDIT
                                ):
                                post_revision = memo.activity.content_object
                                request.user.approve_post_revision(post_revision)
                                memo.delete()

                    #elif action_type == 'close':
                    #    for memo in memo_set:
                    #        if memo.activity.content_object.post_type == "question":
                    #            request.user.close_question(question = memo.activity.content_object, reason = 7)
                    #            memo.delete()
                    elif action_type == 'delete_post':
                        for memo in memo_set:
                            content_object = memo.activity.content_object
                            if isinstance(content_object, models.PostRevision):
                                post = content_object.post
                            else:
                                post = content_object
                            request.user.delete_post(post)
                            reject_reason = models.PostFlagReason.objects.get(
                                                    id = post_data['reject_reason_id']
                                                )
                            body_text = string_concat(
                                _('Your post (copied in the end),'),
                                '<br/>',
                                _('was rejected for the following reason:'),
                                '<br/><br/>',
                                reject_reason.details.html,
                                '<br/><br/>',
                                _('Here is your original post'),
                                '<br/><br/>',
                                post.text
                            )
                            mail.send_mail(
                                subject_line = _('your post was not accepted'),
                                body_text = unicode(body_text),
                                recipient_list = [post.author.email,]
                            )
                            memo.delete()

                    user.update_response_counts()

                    response_data['success'] = True
                    data = simplejson.dumps(response_data)
                    return HttpResponse(data, mimetype="application/json")
                else:
                    raise exceptions.PermissionDenied(
                        _('Sorry, but anonymous users cannot access the inbox')
                    )
            else:
                raise exceptions.PermissionDenied('must use POST request')
        else:
            #todo: show error page but no-one is likely to get here
            return HttpResponseRedirect(reverse('index'))
    except Exception, e:
        message = unicode(e)
        if message == '':
            message = _('Oops, apologies - there was some error')
        response_data['message'] = message
        response_data['success'] = False
        data = simplejson.dumps(response_data)
        return HttpResponse(data, mimetype="application/json")


def process_vote(user = None, vote_direction = None, post = None):
    """function (non-view) that actually processes user votes
    - i.e. up- or down- votes

    in the future this needs to be converted into a real view function
    for that url and javascript will need to be adjusted

    also in the future make keys in response data be more meaningful
    right now they are kind of cryptic - "status", "count"
    """
    if user.is_anonymous():
        raise exceptions.PermissionDenied(_(
            'Sorry, anonymous users cannot vote'
        ))

    user.assert_can_vote_for_post(post = post, direction = vote_direction)
    vote = user.get_old_vote_for_post(post)
    response_data = {}
    if vote != None:
        user.assert_can_revoke_old_vote(vote)
        score_delta = vote.cancel()
        response_data['count'] = post.score + score_delta
        response_data['status'] = 1 #this means "cancel"

    else:
        #this is a new vote
        votes_left = user.get_unused_votes_today()
        if votes_left <= 0:
            raise exceptions.PermissionDenied(
                            _('Sorry you ran out of votes for today')
                        )

        votes_left -= 1
        if votes_left <= \
            askbot_settings.VOTES_LEFT_WARNING_THRESHOLD:
            msg = _('You have %(votes_left)s votes left for today') \
                    % {'votes_left': votes_left }
            response_data['message'] = msg

        if vote_direction == 'up':
            vote = user.upvote(post = post)
        else:
            vote = user.downvote(post = post)

        response_data['count'] = post.score
        response_data['status'] = 0 #this means "not cancel", normal operation

    response_data['success'] = 1

    return response_data


@csrf.csrf_exempt
def vote(request, id):
    """
    todo: this subroutine needs serious refactoring it's too long and is hard to understand

    vote_type:
        acceptAnswer : 0,
        questionUpVote : 1,
        questionDownVote : 2,
        favorite : 4,
        answerUpVote: 5,
        answerDownVote:6,
        offensiveQuestion : 7,
        remove offensiveQuestion flag : 7.5,
        remove all offensiveQuestion flag : 7.6,
        offensiveAnswer:8,
        remove offensiveAnswer flag : 8.5,
        remove all offensiveAnswer flag : 8.6,
        removeQuestion: 9,
        removeAnswer:10
        questionSubscribeUpdates:11
        questionUnSubscribeUpdates:12

    accept answer code:
        response_data['allowed'] = -1, Accept his own answer   0, no allowed - Anonymous    1, Allowed - by default
        response_data['success'] =  0, failed                                               1, Success - by default
        response_data['status']  =  0, By default                       1, Answer has been accepted already(Cancel)

    vote code:
        allowed = -3, Don't have enough votes left
                  -2, Don't have enough reputation score
                  -1, Vote his own post
                   0, no allowed - Anonymous
                   1, Allowed - by default
        status  =  0, By default
                   1, Cancel
                   2, Vote is too old to be canceled

    offensive code:
        allowed = -3, Don't have enough flags left
                  -2, Don't have enough reputation score to do this
                   0, not allowed
                   1, allowed
        status  =  0, by default
                   1, can't do it again
    """
    response_data = {
        "allowed": 1,
        "success": 1,
        "status" : 0,
        "count"  : 0,
        "message" : ''
    }

    try:
        if request.is_ajax() and request.method == 'POST':
            vote_type = request.POST.get('type')
        else:
            raise Exception(_('Sorry, something is not right here...'))

        if vote_type == '0':
            if request.user.is_authenticated():
                answer_id = request.POST.get('postId')
                answer = get_object_or_404(models.Post, post_type='answer', id = answer_id)
                # make sure question author is current user
                if answer.accepted():
                    request.user.unaccept_best_answer(answer)
                    response_data['status'] = 1 #cancelation
                else:
                    request.user.accept_best_answer(answer)

                ####################################################################
                answer.thread.update_summary_html() # regenerate question/thread summary html
                ####################################################################

            else:
                raise exceptions.PermissionDenied(
                        _('Sorry, but anonymous users cannot accept answers')
                    )

        elif vote_type in ('1', '2', '5', '6'):#Q&A up/down votes

            ###############################
            # all this can be avoided with
            # better query parameters
            vote_direction = 'up'
            if vote_type in ('2','6'):
                vote_direction = 'down'

            if vote_type in ('5', '6'):
                #todo: fix this weirdness - why postId here
                #and not with question?
                id = request.POST.get('postId')
                post = get_object_or_404(models.Post, post_type='answer', id=id)
            else:
                post = get_object_or_404(models.Post, post_type='question', id=id)
            #
            ######################

            response_data = process_vote(
                                        user = request.user,
                                        vote_direction = vote_direction,
                                        post = post
                                    )

            ####################################################################
            if vote_type in ('1', '2'): # up/down-vote question
                post.thread.update_summary_html() # regenerate question/thread summary html
            ####################################################################

        elif vote_type in ['7', '8']:
            #flag question or answer
            if vote_type == '7':
                post = get_object_or_404(models.Post, post_type='question', id=id)
            if vote_type == '8':
                id = request.POST.get('postId')
                post = get_object_or_404(models.Post, post_type='answer', id=id)

            request.user.flag_post(post)

            response_data['count'] = post.offensive_flag_count
            response_data['success'] = 1

        elif vote_type in ['7.5', '8.5']:
            #flag question or answer
            if vote_type == '7.5':
                post = get_object_or_404(models.Post, post_type='question', id=id)
            if vote_type == '8.5':
                id = request.POST.get('postId')
                post = get_object_or_404(models.Post, post_type='answer', id=id)

            request.user.flag_post(post, cancel = True)

            response_data['count'] = post.offensive_flag_count
            response_data['success'] = 1
        
        elif vote_type in ['7.6', '8.6']:
            #flag question or answer
            if vote_type == '7.6':
                post = get_object_or_404(models.Post, id=id)
            if vote_type == '8.6':
                id = request.POST.get('postId')
                post = get_object_or_404(models.Post, id=id)

            request.user.flag_post(post, cancel_all = True)

            response_data['count'] = post.offensive_flag_count
            response_data['success'] = 1

        elif vote_type in ['9', '10']:
            #delete question or answer
            post = get_object_or_404(models.Post, post_type='question', id=id)
            if vote_type == '10':
                id = request.POST.get('postId')
                post = get_object_or_404(models.Post, post_type='answer', id=id)

            if post.deleted == True:
                request.user.restore_post(post = post)
            else:
                request.user.delete_post(post = post)

        elif request.is_ajax() and request.method == 'POST':

            if not request.user.is_authenticated():
                response_data['allowed'] = 0
                response_data['success'] = 0

            question = get_object_or_404(models.Post, post_type='question', id=id)
            vote_type = request.POST.get('type')

            #accept answer
            if vote_type == '4':
                fave = request.user.toggle_favorite_question(question)
                response_data['count'] = models.FavoriteQuestion.objects.filter(thread = question.thread).count()
                if fave == False:
                    response_data['status'] = 1

            elif vote_type == '11':#subscribe q updates
                user = request.user
                if user.is_authenticated():
                    if user not in question.thread.followed_by.all():
                        user.follow_question(question)
                        if askbot_settings.EMAIL_VALIDATION == True \
                            and user.email_isvalid == False:

                            response_data['message'] = \
                                    _(
                                        'Your subscription is saved, but email address '
                                        '%(email)s needs to be validated, please see '
                                        '<a href="%(details_url)s">more details here</a>'
                                    ) % {'email':user.email,'details_url':reverse('faq') + '#validate'}

                    subscribed = user.subscribe_for_followed_question_alerts()
                    if subscribed:
                        if 'message' in response_data:
                            response_data['message'] += '<br/>'
                        response_data['message'] += _('email update frequency has been set to daily')
                    #response_data['status'] = 1
                    #responst_data['allowed'] = 1
                else:
                    pass
                    #response_data['status'] = 0
                    #response_data['allowed'] = 0
            elif vote_type == '12':#unsubscribe q updates
                user = request.user
                if user.is_authenticated():
                    user.unfollow_question(question)
        else:
            response_data['success'] = 0
            response_data['message'] = u'Request mode is not supported. Please try again.'

        if vote_type not in (1, 2, 4, 5, 6, 11, 12):
            #favorite or subscribe/unsubscribe
            #upvote or downvote question or answer - those
            #are handled within user.upvote and user.downvote
            post = models.Post.objects.get(id = id)
            post.thread.invalidate_cached_data()

        data = simplejson.dumps(response_data)

    except Exception, e:
        response_data['message'] = unicode(e)
        response_data['success'] = 0
        data = simplejson.dumps(response_data)
    return HttpResponse(data, mimetype="application/json")

#internally grouped views - used by the tagging system
@csrf.csrf_exempt
@decorators.post_only
@decorators.ajax_login_required
def mark_tag(request, **kwargs):#tagging system
    action = kwargs['action']
    post_data = simplejson.loads(request.raw_post_data)
    raw_tagnames = post_data['tagnames']
    reason = post_data['reason']
    assert reason in ('good', 'bad', 'subscribed')
    #separate plain tag names and wildcard tags

    tagnames, wildcards = forms.clean_marked_tagnames(raw_tagnames)
    cleaned_tagnames, cleaned_wildcards = request.user.mark_tags(
                                                            tagnames,
                                                            wildcards,
                                                            reason = reason,
                                                            action = action
                                                        )

    #lastly - calculate tag usage counts
    tag_usage_counts = dict()
    for name in tagnames:
        if name in cleaned_tagnames:
            tag_usage_counts[name] = 1
        else:
            tag_usage_counts[name] = 0

    for name in wildcards:
        if name in cleaned_wildcards:
            tag_usage_counts[name] = models.Tag.objects.filter(
                                        name__startswith = name[:-1]
                                    ).count()
        else:
            tag_usage_counts[name] = 0

    return HttpResponse(simplejson.dumps(tag_usage_counts), mimetype="application/json")

#@decorators.ajax_only
@decorators.get_only
def get_tags_by_wildcard(request):
    """returns an json encoded array of tag names
    in the response to a wildcard tag name
    """
    wildcard = request.GET.get('wildcard', None)
    if wildcard is None:
        raise Http404
        
    matching_tags = models.Tag.objects.get_by_wildcards( [wildcard,] )
    count = matching_tags.count()
    names = matching_tags.values_list('name', flat = True)[:20]
    re_data = simplejson.dumps({'tag_count': count, 'tag_names': list(names)})
    return HttpResponse(re_data, mimetype = 'application/json')

@decorators.ajax_only
def get_html_template(request):
    """returns rendered template"""
    template_name = request.REQUEST.get('template_name', None)
    allowed_templates = (
        'widgets/tag_category_selector.html',
    )
    #have allow simple context for the templates
    if template_name not in allowed_templates:
        raise Http404
    return {
        'html': get_template(template_name).render()
    }

@decorators.get_only
def get_tag_list(request):
    """returns tags to use in the autocomplete
    function
    """
    tag_names = models.Tag.objects.filter(
                        deleted = False,
                        status = models.Tag.STATUS_ACCEPTED
                    ).values_list(
                        'name', flat = True
                    )
    output = '\n'.join(map(escape, tag_names))
    return HttpResponse(output, mimetype = 'text/plain')

@decorators.get_only
def load_tag_wiki_text(request):
    """returns text of the tag wiki in markdown format"""
    if 'tag_id' not in request.GET:
        return HttpResponse('', status = 400)#bad request

    tag = get_object_or_404(models.Tag, id = request.GET['tag_id'])
    tag_wiki_text = getattr(tag.tag_wiki, 'text', '').strip()
    return HttpResponse(tag_wiki_text, mimetype = 'text/plain')

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def save_tag_wiki_text(request):
    """if tag wiki text does not exist,
    creates a new record, otherwise edits an existing
    tag wiki record"""
    form = forms.EditTagWikiForm(request.POST)
    if form.is_valid():
        tag_id = form.cleaned_data['tag_id']
        text = form.cleaned_data['text'] or ' '#a hack to save blank data
        tag = models.Tag.objects.get(id = tag_id)
        if tag.tag_wiki:
            request.user.edit_post(tag.tag_wiki, body_text = text)
            tag_wiki = tag.tag_wiki
        else:
            tag_wiki = request.user.post_tag_wiki(tag, body_text = text)
        return {'html': tag_wiki.html}
    else:
        raise ValueError('invalid post data')

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def rename_tag(request):
    if request.user.is_anonymous() \
        or not request.user.is_administrator_or_moderator():
        raise exceptions.PermissionDenied()
    post_data = simplejson.loads(request.raw_post_data)
    to_name = forms.clean_tag(post_data['to_name'])
    from_name = forms.clean_tag(post_data['from_name'])
    path = post_data['path']

    #kwargs = {'from': old_name, 'to': new_name}
    #call_command('rename_tags', **kwargs)

    tree = category_tree.get_data()
    category_tree.rename_category(
        tree,
        from_name = from_name,
        to_name = to_name,
        path = path
    )
    category_tree.save_data(tree)

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def delete_tag(request):
    """todo: actually delete tags
    now it is only deletion of category from the tree"""
    if request.user.is_anonymous() \
        or not request.user.is_administrator_or_moderator():
        raise exceptions.PermissionDenied()
    post_data = simplejson.loads(request.raw_post_data)
    tag_name = forms.clean_tag(post_data['tag_name'])
    path = post_data['path']
    tree = category_tree.get_data()
    category_tree.delete_category(tree, tag_name, path)
    category_tree.save_data(tree)
    return {'tree_data': tree}

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def add_tag_category(request):
    """adds a category at the tip of a given path expects
    the following keys in the ``request.POST``
    * path - array starting with zero giving path to
      the category page where to add the category
    * new_category_name - string that must satisfy the
      same requiremets as a tag

    return json with the category tree data
    todo: switch to json stored in the live settings
    now we have indented input
    """
    if request.user.is_anonymous() \
        or not request.user.is_administrator_or_moderator():
        raise exceptions.PermissionDenied()

    post_data = simplejson.loads(request.raw_post_data)
    category_name = forms.clean_tag(post_data['new_category_name'])
    path = post_data['path']

    tree = category_tree.get_data()

    if category_tree.path_is_valid(tree, path) == False:
        raise ValueError('category insertion path is invalid')

    new_path = category_tree.add_category(tree, category_name, path)
    category_tree.save_data(tree)
    return {
        'tree_data': tree,
        'new_path': new_path
    }


@decorators.get_only
def get_groups_list(request):
    """returns names of group tags
    for the autocomplete function"""
    group_names = models.Tag.group_tags.get_all().filter(
                                    deleted = False
                                ).values_list(
                                    'name', flat = True
                                )
    group_names = map(lambda v: v.replace('-', ' '), group_names)
    output = '\n'.join(group_names)
    return HttpResponse(output, mimetype = 'text/plain')

@csrf.csrf_protect
def subscribe_for_tags(request):
    """process subscription of users by tags"""
    #todo - use special separator to split tags
    tag_names = request.REQUEST.get('tags','').strip().split()
    pure_tag_names, wildcards = forms.clean_marked_tagnames(tag_names)
    if request.user.is_authenticated():
        if request.method == 'POST':
            if 'ok' in request.POST:
                request.user.mark_tags(
                            pure_tag_names,
                            wildcards,
                            reason = 'good',
                            action = 'add'
                        )
                request.user.message_set.create(
                    message = _('Your tag subscription was saved, thanks!')
                )
            else:
                message = _(
                    'Tag subscription was canceled (<a href="%(url)s">undo</a>).'
                ) % {'url': request.path + '?tags=' + request.REQUEST['tags']}
                request.user.message_set.create(message = message)
            return HttpResponseRedirect(reverse('index'))
        else:
            data = {'tags': tag_names}
            return render_into_skin('subscribe_for_tags.html', data, request)
    else:
        all_tag_names = pure_tag_names + wildcards
        message = _('Please sign in to subscribe for: %(tags)s') \
                    % {'tags': ', '.join(all_tag_names)}
        request.user.message_set.create(message = message)
        request.session['subscribe_for_tags'] = (pure_tag_names, wildcards)
        return HttpResponseRedirect(url_utils.get_login_url())


@decorators.get_only
def api_get_questions(request):
    """json api for retrieving questions"""
    query = request.GET.get('query', '').strip()
    if not query:
        return HttpResponseBadRequest('Invalid query')
    threads = models.Thread.objects.get_for_query(query)
    if should_show_sort_by_relevance():
        threads = threads.extra(order_by = ['-relevance'])
    #todo: filter out deleted threads, for now there is no way
    threads = threads.distinct()[:30]
    thread_list = [{
        'title': escape(thread.title),
        'answer_count': thread.get_answer_count(request.user)
    } for thread in threads]
    json_data = simplejson.dumps(thread_list)
    return HttpResponse(json_data, mimetype = "application/json")


@csrf.csrf_exempt
@decorators.post_only
@decorators.ajax_login_required
def set_tag_filter_strategy(request):
    """saves data in the ``User.[email/display]_tag_filter_strategy``
    for the current user
    """
    filter_type = request.POST['filter_type']
    filter_value = int(request.POST['filter_value'])
    assert(filter_type in ('display', 'email'))
    if filter_type == 'display':
        assert(filter_value in dict(const.TAG_DISPLAY_FILTER_STRATEGY_CHOICES))
        request.user.display_tag_filter_strategy = filter_value
    else:
        assert(filter_value in dict(const.TAG_EMAIL_FILTER_STRATEGY_CHOICES))
        request.user.email_tag_filter_strategy = filter_value
    request.user.save()
    return HttpResponse('', mimetype = "application/json")


@login_required
@csrf.csrf_protect
def close(request, id):#close question
    """view to initiate and process
    question close
    """
    question = get_object_or_404(models.Post, post_type='question', id=id)
    try:
        if request.method == 'POST':
            form = forms.CloseForm(request.POST)
            if form.is_valid():
                reason = form.cleaned_data['reason']

                request.user.close_question(
                                        question = question,
                                        reason = reason
                                    )
            return HttpResponseRedirect(question.get_absolute_url())
        else:
            request.user.assert_can_close_question(question)
            form = forms.CloseForm()
            data = {
                'question': question,
                'form': form,
            }
            return render_into_skin('close.html', data, request)
    except exceptions.PermissionDenied, e:
        request.user.message_set.create(message = unicode(e))
        return HttpResponseRedirect(question.get_absolute_url())

@login_required
@csrf.csrf_protect
def reopen(request, id):#re-open question
    """view to initiate and process
    question close

    this is not an ajax view
    """

    question = get_object_or_404(models.Post, post_type='question', id=id)
    # open question
    try:
        if request.method == 'POST' :
            request.user.reopen_question(question)
            return HttpResponseRedirect(question.get_absolute_url())
        else:
            request.user.assert_can_reopen_question(question)
            closed_by_profile_url = question.thread.closed_by.get_profile_url()
            closed_by_username = question.thread.closed_by.username
            data = {
                'question' : question,
                'closed_by_profile_url': closed_by_profile_url,
                'closed_by_username': closed_by_username,
            }
            return render_into_skin('reopen.html', data, request)

    except exceptions.PermissionDenied, e:
        request.user.message_set.create(message = unicode(e))
        return HttpResponseRedirect(question.get_absolute_url())


@csrf.csrf_exempt
@decorators.ajax_only
def swap_question_with_answer(request):
    """receives two json parameters - answer id
    and new question title
    the view is made to be used only by the site administrator
    or moderators
    """
    if request.user.is_authenticated():
        if request.user.is_administrator() or request.user.is_moderator():
            answer = models.Post.objects.get_answers(request.user).get(id = request.POST['answer_id'])
            new_question = answer.swap_with_question(new_title = request.POST['new_title'])
            return {
                'id': new_question.id,
                'slug': new_question.slug
            }
    raise Http404

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def upvote_comment(request):
    if request.user.is_anonymous():
        raise exceptions.PermissionDenied(_('Please sign in to vote'))
    form = forms.VoteForm(request.POST)
    if form.is_valid():
        comment_id = form.cleaned_data['post_id']
        cancel_vote = form.cleaned_data['cancel_vote']
        comment = get_object_or_404(models.Post, post_type='comment', id=comment_id)
        process_vote(
            post = comment,
            vote_direction = 'up',
            user = request.user
        )
    else:
        raise ValueError
    return {'score': comment.score}

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def delete_post(request):
    if request.user.is_anonymous():
        raise exceptions.PermissionDenied(_('Please sign in to delete/restore posts'))
    form = forms.VoteForm(request.POST)
    if form.is_valid():
        post_id = form.cleaned_data['post_id']
        post = get_object_or_404(
            models.Post,
            post_type__in = ('question', 'answer'),
            id = post_id
        )
        if form.cleaned_data['cancel_vote']:
            request.user.restore_post(post)
        else:
            request.user.delete_post(post)
    else:
        raise ValueError
    return {'is_deleted': post.deleted}

#askbot-user communication system
@csrf.csrf_exempt
def read_message(request):#marks message a read
    if request.method == "POST":
        if request.POST['formdata'] == 'required':
            request.session['message_silent'] = 1
            if request.user.is_authenticated():
                request.user.delete_messages()
    return HttpResponse('')


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def edit_group_membership(request):
    form = forms.EditGroupMembershipForm(request.POST)
    if form.is_valid():
        group_name = form.cleaned_data['group_name']
        user_id = form.cleaned_data['user_id']
        try:
            user = models.User.objects.get(id = user_id)
        except models.User.DoesNotExist:
            raise exceptions.PermissionDenied(
                'user with id %d not found' % user_id
            )

        action = form.cleaned_data['action']
        #warning: possible race condition
        if action == 'add':
            group_params = {'group_name': group_name, 'user': user}
            group = models.Tag.group_tags.get_or_create(**group_params)
            request.user.edit_group_membership(user, group, 'add')
            template = get_template('widgets/group_snippet.html')
            return {
                'name': group.name,
                'description': getattr(group.tag_wiki, 'text', ''),
                'html': template.render({'group': group})
            }
        elif action == 'remove':
            try:
                group = models.Tag.group_tags.get_by_name(group_name = group_name)
                request.user.edit_group_membership(user, group, 'remove')
            except models.Tag.DoesNotExist:
                raise exceptions.PermissionDenied()
        else:
            raise exceptions.PermissionDenied()
    else:
        raise exceptions.PermissionDenied()


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def save_group_logo_url(request):
    """saves urls for the group logo"""
    form = forms.GroupLogoURLForm(request.POST)    
    if form.is_valid():
        group_id = form.cleaned_data['group_id']
        image_url = form.cleaned_data['image_url']
        group = models.Tag.group_tags.get(id = group_id)
        group.group_profile.logo_url = image_url
        group.group_profile.save()
    else:
        raise ValueError('invalid data found when saving group logo')


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def delete_group_logo(request):
    group_id = IntegerField().clean(int(request.POST['group_id']))
    group = models.Tag.group_tags.get(id = group_id)
    group.group_profile.logo_url = None
    group.group_profile.save()


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def delete_post_reject_reason(request):
    reason_id = IntegerField().clean(int(request.POST['reason_id']))
    reason = models.PostFlagReason.objects.get(id = reason_id)
    reason.delete()


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def toggle_group_profile_property(request):
    #todo: this might be changed to more general "toggle object property"
    group_id = IntegerField().clean(int(request.POST['group_id']))
    property_name = CharField().clean(request.POST['property_name'])
    assert property_name in ('is_open', 'moderate_email')

    group = models.Tag.objects.get(id = group_id)
    new_value = not getattr(group.group_profile, property_name)
    setattr(group.group_profile, property_name, new_value)
    group.group_profile.save()
    return {'is_enabled': new_value}


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.admins_only
def edit_object_property_text(request):
    model_name = CharField().clean(request.REQUEST['model_name'])
    object_id = IntegerField().clean(request.REQUEST['object_id'])
    property_name = CharField().clean(request.REQUEST['property_name'])

    accessible_fields = (
        ('GroupProfile', 'preapproved_emails'),
        ('GroupProfile', 'preapproved_email_domains')
    )

    if (model_name, property_name) not in accessible_fields:
        raise exceptions.PermissionDenied()

    obj = models.get_model(model_name).objects.get(id=object_id)
    if request.method == 'POST':
        text = CharField().clean(request.POST['text'])
        setattr(obj, property_name, text)
        obj.save()
    elif request.method == 'GET':
        return {'text': getattr(obj, property_name)}
    else:
        raise exceptions.PermissionDenied()


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
def join_or_leave_group(request):
    """only current user can join/leave group"""
    if request.user.is_anonymous():
        raise exceptions.PermissionDenied()

    group_id = IntegerField().clean(request.POST['group_id'])
    group = models.Tag.objects.get(id = group_id)

    if request.user.is_group_member(group):
        action = 'remove'
        is_member = False
    else:
        action = 'add'
        is_member = True
    request.user.edit_group_membership(
        user = request.user,
        group = group,
        action = action
    )
    return {'is_member': is_member}


@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def save_post_reject_reason(request):
    """saves post reject reason and returns the reason id
    if reason_id is not given in the input - a new reason is created,
    otherwise a reason with the given id is edited and saved
    """
    form = forms.EditRejectReasonForm(request.POST)
    if form.is_valid():
        title = form.cleaned_data['title']
        details = form.cleaned_data['details']
        if form.cleaned_data['reason_id'] is None:
            reason = request.user.create_post_reject_reason(
                title = title, details = details
            )
        else:
            reason_id = form.cleaned_data['reason_id']
            reason = models.PostFlagReason.objects.get(id = reason_id)
            request.user.edit_post_reject_reason(
                reason, title = title, details = details
            )
        return {
            'reason_id': reason.id,
            'title': title,
            'details': details
        }
    else:
        raise Exception(forms.format_form_errors(form))

@csrf.csrf_exempt
@decorators.ajax_only
@decorators.post_only
@decorators.admins_only
def moderate_suggested_tag(request):
    """accepts or rejects a suggested tag
    if thread id is given, then tag is 
    applied to or removed from only one thread, 
    otherwise the decision applies to all threads
    """
    form = forms.ModerateTagForm(request.POST)
    if form.is_valid():
        tag_id = form.cleaned_data['tag_id']
        thread_id = form.cleaned_data.get('thread_id', None)

        try:
            tag = models.Tag.objects.get(id = tag_id)#can tag not exist?
        except models.Tag.DoesNotExist:
            return

        if thread_id:
            threads = models.Thread.objects.filter(id = thread_id)
        else:
            threads = tag.threads.all()

        if form.cleaned_data['action'] == 'accept':
            #todo: here we lose ability to come back
            #to the tag moderation and approve tag to
            #other threads later for the case where tag.used_count > 1
            tag.status = models.Tag.STATUS_ACCEPTED
            tag.save()
            for thread in threads:
                thread.add_tag(
                    tag_name = tag.name,
                    user = tag.created_by,
                    timestamp = datetime.datetime.now(),
                    silent = True
                )
        else:
            if tag.threads.count() > len(threads):
                for thread in threads:
                    thread.tags.remove(tag)
                tag.used_count = tag.threads.count()
                tag.save()
            elif tag.status == models.Tag.STATUS_SUGGESTED:
                tag.delete()
    else:
        raise Exception(forms.format_form_errors(form))
