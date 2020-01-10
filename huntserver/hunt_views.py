from datetime import datetime
from dateutil import tz
from django.conf import settings
from ratelimit.utils import is_ratelimited
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseNotFound, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render, redirect
from django.template import Template, RequestContext
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import smart_str
import json
import os
import re

from .models import Puzzle, Hunt, Submission, Message, Unlockable, Prepuzzle, Hint
from .forms import AnswerForm, HintRequestForm
from .utils import respond_to_submission, team_from_user_hunt, dummy_team_from_hunt
from .info_views import current_hunt_info

import logging
logger = logging.getLogger(__name__)


def protected_static(request, file_path):
    """
    A view to serve protected static content. Does a permission check and if it passes,
    the file is served via X-Sendfile.
    """

    allowed = False
    levels = file_path.split("/")
    if(levels[0] == "puzzles"):
        puzzle_id = levels[1].split("-")[0].split(".")[0]
        puzzle = get_object_or_404(Puzzle, puzzle_id=puzzle_id)
        hunt = puzzle.hunt
        user = request.user
        if (hunt.is_public or user.is_staff):
            allowed = True
        else:
            team = team_from_user_hunt(user, hunt)
            if (team is not None and puzzle in team.unlocked.all()):
                allowed = True
    elif(levels[0] == "solutions"):
        puzzle_id = levels[1].split("-")[0].split("_")[0]
        hunt = get_object_or_404(Puzzle, puzzle_id=puzzle_id).hunt
        if (hunt.is_public or user.is_staff):
            allowed = True
    else:
        allowed = True

    if allowed:
        response = HttpResponse()
        # let apache determine the correct content type
        response['Content-Type'] = ""
        # This is what lets django access the normally restricted /media/
        response['X-Sendfile'] = smart_str(os.path.join(settings.MEDIA_ROOT, file_path))
        return response
    else:
        logger.info("User %s tried to access %s and failed." % (str(request.user), file_path))

    return HttpResponseNotFound('<h1>Page not found</h1>')


def hunt(request, hunt_num):
    """
    The main view to render hunt templates. Does various permission checks to determine the set
    of puzzles to display and then renders the string in the hunt's "template" field to HTML.
    """

    hunt = get_object_or_404(Hunt, hunt_number=hunt_num)
    team = team_from_user_hunt(request.user, hunt)

    # Admins get all access, wrong teams/early lookers get an error page
    # real teams get appropriate puzzles, and puzzles from past hunts are public
    if (hunt.is_public or request.user.is_staff):
        puzzle_list = hunt.puzzle_set.all()

    elif(team and team.is_playtester_team):
        puzzle_list = team.unlocked.filter(hunt=hunt)

    # Hunt has not yet started
    elif(hunt.is_locked):
        if(hunt.is_day_of_hunt):
            return render(request, 'access_error.html', {'reason': "hunt"})
        else:
            return hunt_prepuzzle(request, hunt_num)

    # Hunt has started
    elif(hunt.is_open):
        # see if the team does not belong to the hunt being accessed
        if (not request.user.is_authenticated):
            return redirect('%s?next=%s' % (settings.LOGIN_URL, request.path))

        elif(team is None or (team.hunt != hunt)):
            return render(request, 'access_error.html', {'reason': "team"})
        else:
            puzzle_list = team.unlocked.filter(hunt=hunt)

        # No else case, all 3 possible hunt states have been checked.

    puzzles = sorted(puzzle_list, key=lambda p: p.puzzle_number)
    if(team is None):
        solved = []
    else:
        solved = team.solved.all()
    context = {'hunt': hunt, 'puzzles': puzzles, 'team': team, 'solved': solved}

    return HttpResponse(Template(hunt.template).render(RequestContext(request, context)))


def current_hunt(request):
    """ A simple view that calls ``huntserver.hunt_views.hunt`` with the current hunt's number. """
    return hunt(request, Hunt.objects.get(is_current_hunt=True).hunt_number)


def prepuzzle(request, prepuzzle_num):
    """
    A view to handle answer submissions via POST and render the basic prepuzzle page rendering.
    """

    puzzle = Prepuzzle.objects.get(pk=prepuzzle_num)

    # Dealing with answer submissions, proper procedure is to create a submission
    # object and then rely on utils.respond_to_submission for automatic responses.
    if request.method == 'POST':
        form = AnswerForm(request.POST)
        if form.is_valid():
            user_answer = re.sub("[ _\-;:+,.!?]", "", form.cleaned_data['answer'])

            # Compare against correct answer
            if(puzzle.answer.lower() == user_answer.lower()):
                is_correct = True
                response = puzzle.response_string
                logger.info("User %s solved prepuzzle %s." % (str(request.user), prepuzzle_num))
            else:
                is_correct = False
                response = ""
        else:
            is_correct = None
            response = ""
        response_vars = {'response': response, 'is_correct': is_correct}
        return HttpResponse(json.dumps(response_vars))

    else:
        if(not (puzzle.released or request.user.is_staff)):
            return current_hunt_info(request)
        form = AnswerForm()
        context = {'form': form, 'puzzle': puzzle}
        return HttpResponse(Template(puzzle.template).render(RequestContext(request, context)))


def hunt_prepuzzle(request, hunt_num):
    """
    A simple view that locates the correct prepuzzle for a hunt and redirects there if it exists.
    """
    curr_hunt = get_object_or_404(Hunt, hunt_number=hunt_num)
    if(hasattr(curr_hunt, "prepuzzle")):
        return prepuzzle(request, curr_hunt.prepuzzle.pk)
    else:
        # Maybe we can do something better, but for now, redirect to the main page
        return current_hunt_info(request)


def current_prepuzzle(request):
    """
    A simple view that locates the correct prepuzzle for the current hunt and redirects there if it exists.
    """
    return hunt_prepuzzle(request, Hunt.objects.get(is_current_hunt=True).hunt_number)


def get_ratelimit_key(group, request):
    return request.ratelimit_key


def puzzle_view(request, puzzle_id):
    """
    A view to handle answer submissions via POST, handle response update requests via AJAX, and
    render the basic per-puzzle pages.
    """
    puzzle = get_object_or_404(Puzzle, puzzle_id__iexact=puzzle_id)
    team = team_from_user_hunt(request.user, puzzle.hunt)

    if(team is not None):
        request.ratelimit_key = team.team_name

    is_ratelimited(request, fn=puzzle_view, key='user', rate='2/10s', method='POST', increment=True)
    is_ratelimited(request, fn=puzzle_view, key=get_ratelimit_key, rate='5/m', method='POST', increment=True)

    if(getattr(request, 'limited', False)):
        logger.info("User %s rate-limited for puzzle %s" % (str(request.user), puzzle_id))
        return HttpResponseForbidden()

    # Dealing with answer submissions, proper procedure is to create a submission
    # object and then rely on utils.respond_to_submission for automatic responses.
    if request.method == 'POST':
        # Deal with answers from archived hunts
        if(puzzle.hunt.is_public):
            form = AnswerForm(request.POST)
            team = dummy_team_from_hunt(puzzle.hunt)
            if form.is_valid():
                user_answer = re.sub("[ _\-;:+,.!?]", "", form.cleaned_data['answer'])
                s = Submission.objects.create(submission_text=user_answer,
                    puzzle=puzzle, submission_time=timezone.now(), team=team)
                response = respond_to_submission(s)
                is_correct = s.is_correct
            else:
                response = "Invalid Submission"
                is_correct = None
            context = {'form': form, 'pages': list(range(puzzle.num_pages)),
                      'puzzle': puzzle, 'PROTECTED_URL': settings.PROTECTED_URL,
                      'response': response, 'is_correct': is_correct}
            return render(request, 'puzzle.html', context)

        # If the hunt isn't public and you aren't signed in, please stop...
        if(team is None):
            return HttpResponse('fail')

        # Normal answer responses for a signed in user in an ongoing hunt
        form = AnswerForm(request.POST)
        if form.is_valid():
            user_answer = re.sub("[ _\-;:+,.!?]", "", form.cleaned_data['answer'])
            s = Submission.objects.create(submission_text=user_answer,
                puzzle=puzzle, submission_time=timezone.now(), team=team)
            response = respond_to_submission(s)

        # Render response to HTML
        submission_list = [render_to_string('puzzle_sub_row.html', {'submission': s})]

        try:
            last_date = Submission.objects.latest('modified_date').modified_date.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except:
            last_date = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        # Send back rendered response for display
        context = {'submission_list': submission_list, 'last_date': last_date}
        return HttpResponse(json.dumps(context))

    # Will return HTML rows for all submissions the user does not yet have
    elif request.is_ajax():
        if(team is None):
            return HttpResponseNotFound('access denied')

        # Find which objects the user hasn't seen yet and render them to HTML
        last_date = datetime.strptime(request.GET.get("last_date"), '%Y-%m-%dT%H:%M:%S.%fZ')
        last_date = last_date.replace(tzinfo=tz.gettz('UTC'))
        submissions = Submission.objects.filter(modified_date__gt=last_date)
        submissions = submissions.filter(team=team, puzzle=puzzle)
        submission_list = [render_to_string('puzzle_sub_row.html', {'submission': submission}) for submission in submissions]

        try:
            last_date = Submission.objects.latest('modified_date').modified_date.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except:
            last_date = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        context = {'submission_list': submission_list, 'last_date': last_date}
        return HttpResponse(json.dumps(context))

    else:
        # Only allowed access if the hunt is public or if unlocked by team
        if(not puzzle.hunt.is_public):
            if(not request.user.is_authenticated):
                return redirect('%s?next=%s' % (settings.LOGIN_URL, request.path))

            if (not request.user.is_staff):
                if(team is None or puzzle not in team.unlocked.all()):
                    return render(request, 'access_error.html', {'reason': "puzzle"})

        # The logic above is negated to weed out edge cases, so here is a summary:
        # If we've made it here, the hunt is public OR the user is staff OR
        # the user 1) is signed in, 2) not staff, 3) is on a team, and 4) has access
        submissions = puzzle.submission_set.filter(team=team).order_by('pk')
        form = AnswerForm()
        try:
            last_date = Submission.objects.latest('modified_date').modified_date.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except:
            last_date = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        context = {'form': form, 'pages': list(range(puzzle.num_pages)), 'puzzle': puzzle,
                   'submission_list': submissions, 'PROTECTED_URL': settings.PROTECTED_URL,
                   'last_date': last_date, 'team': team}
        return render(request, 'puzzle.html', context)


@login_required
def puzzle_hint(request, puzzle_id):
    """
    A view to handle hint requests via POST, handle response update requests via AJAX, and
    render the basic puzzle-hint pages.
    """
    puzzle = get_object_or_404(Puzzle, puzzle_id__iexact=puzzle_id)
    team = team_from_user_hunt(request.user, puzzle.hunt)

    if request.method == 'POST':
        # If the hunt isn't public and you aren't signed in, please stop...
        if(team is None):
            return HttpResponse('fail')

        # Normal answer responses for a signed in user in an ongoing hunt
        form = HintRequestForm(request.POST)
        if form.is_valid():
            h = Hint.objects.create(request=form.cleaned_data['request'], puzzle=puzzle, team=team,
                                    request_time=timezone.now(), last_modified_time=timezone.now())

        # Render response to HTML
        hint_list = [render_to_string('hint_row.html', {'hint': h})]

        try:
            last_hint = Hint.objects.latest('last_modified_time')
            last_date = last_hint.last_modified_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except Hint.DoesNotExist:
            last_date = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        # Send back rendered response for display
        context = {'hint_list': hint_list, 'last_date': last_date}
        return HttpResponse(json.dumps(context))

    # Will return HTML rows for all submissions the user does not yet have
    elif request.is_ajax():
        if(team is None):
            return HttpResponseNotFound('access denied')

        # Find which objects the user hasn't seen yet and render them to HTML
        last_date = datetime.strptime(request.GET.get("last_date"), '%Y-%m-%dT%H:%M:%S.%fZ')
        last_date = last_date.replace(tzinfo=tz.gettz('UTC'))
        hints = Hint.objects.filter(last_modified_time__gt=last_date)
        hints = hints.filter(team=team, puzzle=puzzle)
        hint_list = [render_to_string('hint_row.html', {'hint': hint}) for hint in hints]

        try:
            last_hint = Hint.objects.latest('last_modified_time')
            last_date = last_hint.last_modified_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except Hint.DoesNotExist:
            last_date = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        context = {'hint_list': hint_list, 'last_date': last_date}
        return HttpResponse(json.dumps(context))

    else:
        if(team is None or puzzle not in team.unlocked.all()):
            return render(request, 'access_error.html', {'reason': "puzzle"})

        form = HintRequestForm()
        hints = team.hint_set.filter(puzzle=puzzle).order_by('pk')
        try:
            last_hint = Hint.objects.latest('last_modified_time')
            last_date = last_hint.last_modified_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        except Hint.DoesNotExist:
            last_date = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        context = {'form': form, 'pages': list(range(puzzle.num_pages)), 'puzzle': puzzle,
                   'hint_list': hints, 'last_date': last_date, 'team': team}
        return render(request, 'puzzle_hint.html', context)


@login_required
def chat(request):
    """
    A view to handle message submissions via POST, handle message update requests via AJAX, and
    render the hunt participant view of the chat.
    """
    curr_hunt = Hunt.objects.get(is_current_hunt=True)
    team = team_from_user_hunt(request.user, curr_hunt)
    if request.method == 'POST':
        # There is data in the post request, but we don't need anything but
        #   the message because normal users can't send as staff or other teams
        m = Message.objects.create(time=timezone.now(), text=request.POST.get('message'),
                                   is_response=False, team=team)
        team.last_received_message = m.pk
        messages = [m]
    else:
        if(team is None):
            return render(request, 'access_error.html', {'reason': "team"})
        if request.is_ajax():
            messages = Message.objects.filter(pk__gt=request.GET.get("last_pk"))
        else:
            messages = Message.objects
        messages = messages.filter(team=team).order_by('time')

    # The whole message_dict format is for ajax/template uniformity
    rendered_messages = render_to_string('chat_messages.html',
        {'messages': messages, 'team_name': team.team_name})
    message_dict = {team.team_name: {'pk': team.pk, 'messages': rendered_messages}}
    try:
        last_pk = Message.objects.latest('id').id
    except Message.DoesNotExist:
        last_pk = 0
    team.last_seen_message = last_pk

    team.save()  # Save last_*_message vars
    context = {'message_dict': message_dict, 'last_pk': last_pk}
    if request.is_ajax() or request.method == 'POST':
        return HttpResponse(json.dumps(context))
    else:
        context['team'] = team
        return render(request, 'chat.html', context)


@login_required
def unlockables(request):
    """ A view to render the unlockables page for hunt participants. """
    curr_hunt = Hunt.objects.get(is_current_hunt=True)
    team = team_from_user_hunt(request.user, curr_hunt)
    if(team is None):
        return render(request, 'access_error.html', {'reason': "team"})
    unlockables = Unlockable.objects.filter(puzzle__in=team.solved.all())
    return render(request, 'unlockables.html', {'unlockables': unlockables, 'team': team})
