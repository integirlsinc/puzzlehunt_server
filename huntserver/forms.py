from django import forms

class AnswerForm(forms.Form):
    answer = forms.CharField(max_length=100, label='Answer')

class SubmissionForm(forms.Form):
    response = forms.CharField(max_length=400, label='response', initial="Wrong Answer")
    sub_id = forms.CharField(label='sub_id')

class UnlockForm(forms.Form):
    team_id = forms.CharField(label='team_id')
    puzzle_id = forms.CharField(label='puzzle_id')