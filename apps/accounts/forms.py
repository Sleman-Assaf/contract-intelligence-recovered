"""Account forms (§34).

Registration is deliberately thin: an email and a password, nothing else. Role
and entity decide what a person may see and approve, so neither can be
self-declared -- an account arrives with the least privilege the system has
and stays inert until somebody with MANAGE_USERS turns it on.
"""
from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext_lazy as _

from .models import Role, User


class LoginForm(AuthenticationForm):
    """Sign in, with one extra answer: the account exists but is not yet on.

    Django cannot tell the difference by itself -- `ModelBackend` refuses an
    inactive user before the form sees it, so every failure looks identical.

    The message is given ONLY when the password is also correct. Saying "that
    account is awaiting activation" to anyone who types an address would turn
    this page into a way of discovering which addresses are real; requiring
    the password means it is only ever told to the person who already owns the
    account, which is the person who needs to know.
    """

    inactive_message = _(
        "لم يُفعّل مسؤول النظام حسابك بعد. تواصل مع مسؤول النظام لتفعيله."
    )
    locked_message = _(
        "محاولات دخول خاطئة كثيرة. انتظر ١٥ دقيقة ثم حاول مرة أخرى."
    )

    def clean(self):
        from .throttle import is_locked

        # Before anything checks a password -- the inactive-account answer
        # below checks one too.
        if is_locked(self.request, self.data.get("username") or ""):
            raise forms.ValidationError(self.locked_message, code="locked")
        try:
            return super().clean()
        except forms.ValidationError:
            email = (self.cleaned_data.get("username")
                     or self.data.get("username") or "").strip().lower()
            password = self.data.get("password") or ""
            account = User.objects.filter(email=email).first()
            if account and not account.is_active and account.check_password(password):
                raise forms.ValidationError(self.inactive_message, code="inactive")
            raise


class SignUpForm(forms.ModelForm):
    """Email and password. Everything else is an administrator's decision."""

    password1 = forms.CharField(
        label=_("كلمة المرور"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )
    password2 = forms.CharField(
        label=_("تأكيد كلمة المرور"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
        help_text=_("أعد كتابة كلمة المرور للتأكد من عدم وجود خطأ مطبعي."),
    )

    class Meta:
        model = User
        fields = ["username", "email"]
        widgets = {
            "username": forms.TextInput(attrs={
                "autocomplete": "name", "autofocus": True,
                "placeholder": _("مثال: أحمد العلي"),
            }),
            "email": forms.EmailInput(attrs={
                "autocomplete": "email",
                "placeholder": "name@ajigroup.com",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Blank on the model, because accounts created before this field
        # existed cannot be given a name retroactively. Required here, so
        # every account created from now on arrives with one.
        self.fields["username"].required = True
        self.fields["username"].label = _("اسم المستخدم")
        self.fields["username"].help_text = _(
            "الاسم الذي يراه زملاؤك في الموقع. تسجيل الدخول يبقى بالبريد."
        )

    def clean_username(self):
        """Collapse the whitespace, and nothing else.

        Not lower-cased and not made unique: this is what a person is called,
        not a key. Two colleagues may share a name and still own different
        accounts, because the account is the address.
        """
        name = " ".join((self.cleaned_data.get("username") or "").split())
        if len(name) < 2:
            raise forms.ValidationError(_("اكتب اسماً من حرفين على الأقل."))
        return name

    def clean_email(self):
        # Stored lower-cased: the address is the username, and two accounts
        # differing only in capitalisation would be two people to the database
        # and one person to everybody else.
        email = (self.cleaned_data.get("email") or "").strip().lower()

        allowed = [
            d.strip().lower().lstrip("@")
            for d in getattr(settings, "SIGNUP_ALLOWED_DOMAINS", [])
            if d.strip()
        ]
        if allowed and email.rsplit("@", 1)[-1] not in allowed:
            raise forms.ValidationError(
                _("التسجيل متاح لعناوين البريد التابعة لـ %(domains)s فقط.")
                % {"domains": "، ".join("@" + d for d in allowed)}
            )
        return email

    def clean_password2(self):
        p1 = self.cleaned_data.get("password1")
        p2 = self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError(_("كلمتا المرور غير متطابقتين."))
        # Run the project's validators against the address too, so "aji@aji.com"
        # cannot be its own password.
        password_validation.validate_password(p2, self.instance)
        return p2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        # The two lines that matter. A self-registered account can do nothing
        # until an administrator activates it, and arrives with the lowest
        # role rather than inheriting one from whoever invited them.
        user.is_active = False
        user.role = Role.USER
        if commit:
            user.save()
        return user
