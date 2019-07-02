import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from django_models_from_csv import models
from django_models_from_csv.exceptions import UniqueColumnError
from django_models_from_csv.forms import SchemaRefineForm
from django_models_from_csv.utils.common import get_setting
from django_models_from_csv.utils.csv import fetch_csv
from django_models_from_csv.utils.importing import import_records
from django_models_from_csv.utils.dynmodel import (
    from_csv_url, from_screendoor, from_private_sheet
)
from django_models_from_csv.utils.screendoor import ScreendoorImporter
from django_models_from_csv.utils.google_sheets import (
   GoogleOAuth, PrivateSheetImporter
)


logger = logging.getLogger(__name__)


@login_required
def begin(request):
    """
    Entry point for setting up the rest of the system. At this point
    the user has logged in using the default login and are now getting
    ready to configure the database, schema (via Google sheets URL) and
    any authentication backends (Google Oauth2, Slack, etc).
    """
    if request.method == "GET":
        # Don't go back into this flow if we've already done it
        addnew = request.GET.get("addnew")
        models_count = models.DynamicModel.objects.count()
        if addnew:
            return render(request, 'begin.html', {})
        elif models_count:
            return redirect('/admin/')
        return render(request, 'begin.html', {})
    elif  request.method == "POST":
        # get params from request
        csv_url = request.POST.get("csv_url")
        csv_google_sheets_auth_code = request.POST.get(
            "csv_google_sheets_auth_code"
        )
        sd_api_key = request.POST.get("sd_api_key")
        sd_project_id = request.POST.get("sd_project_id")
        sd_form_id = request.POST.get("sd_form_id")
        try:
            if csv_url and csv_google_sheets_auth_code:
                name = request.POST.get("csv_name")
                dynmodel = from_private_sheet(
                    name, csv_url, auth_code=csv_google_sheets_auth_code,
                )
            elif csv_url:
                name = request.POST.get("csv_name")
                dynmodel = from_csv_url(
                    name, csv_url,
                    csv_google_sheets_auth_code=csv_google_sheets_auth_code
                )
            elif sd_api_key:
                name = request.POST.get("sd_name")
                dynmodel = from_screendoor(
                    name,
                    sd_api_key,
                    int(sd_project_id),
                    form_id=int(sd_form_id) if sd_form_id else None
                )
        except UniqueColumnError as e:
            return render(request, 'begin.html', {
                "errors": str(e)
            })
        return redirect('csv_models:refine-and-import', dynmodel.id)


@login_required
def refine_and_import(request, id):
    """
    Allow the user to modify the auto-generated column types and
    names. This is done before we import the dynmodel data.

    If this succeeds, we do some preliminary checks against the
    CSV file to make sure there aren't duplicate headers/etc.
    Then we do the import. On success, this redirects to the URL
    specified by the CSV_MODELS_WIZARD_REDIRECT_TO setting if
    it exists.
    """
    dynmodel = get_object_or_404(models.DynamicModel, id=id)
    if request.method == "GET":
        refine_form = SchemaRefineForm({
            "columns": dynmodel.columns
        })
        return render(request, 'refine-and-import.html', {
            "form": refine_form,
            "dynmodel": dynmodel,
        })
    elif  request.method == "POST":
        refine_form = SchemaRefineForm(request.POST)
        if not refine_form.is_valid():
            return render(request, 'refine-and-import.html', {
                "form": refine_form,
                "dynmodel": dynmodel,
            })

        columns = refine_form.cleaned_data["columns"]
        dynmodel.columns = columns
        # Alter the DB
        dynmodel.save()
        dynmodel.refresh_from_db()

        # Now perform the import
        if dynmodel.csv_url and dynmodel.csv_google_refresh_token:
            oauther = GoogleOAuth(
                get_setting("GOOGLE_CLIENT_ID"),
                get_setting("GOOGLE_CLIENT_SECRET")
            )
            access_data = oauther.get_access_data(
                refresh_token=dynmodel.csv_google_refresh_token
            )
            token = access_data["access_token"]
            csv = PrivateSheetImporter(token).get_csv_from_url(
                dynmodel.csv_url
            )
        elif dynmodel.csv_url:
            csv = fetch_csv(dynmodel.csv_url)
        elif dynmodel.sd_api_key:
            importer = ScreendoorImporter(api_key=dynmodel.sd_api_key)
            csv = importer.build_csv(
                dynmodel.sd_project_id, form_id=dynmodel.sd_form_id
            )
        else:
            raise NotImplementedError("Invalid data source for %s" % dynmodel)

        # Handle import errors
        Model = dynmodel.get_model()
        errors = import_records(csv, Model, dynmodel)
        logger.error("Import errors: %s" % errors)
        if errors:
            return render(request, 'refine-and-import.html', {
                "form": refine_form,
                "dynmodel": dynmodel,
                "errors": errors,
            })

        next = get_setting("CSV_MODELS_WIZARD_REDIRECT_TO")
        if next:
            return redirect(next)

        return render(request, "import-complete.html", {
            "dynmodel": dynmodel,
            "n_records": Model.objects.count(),
        })


@login_required
def refine_and_import_by_name(request, name):
    id = models.DynamicModel.objects.get_or_404(name=name)
    return refine_and_import(request, id)


@login_required
def import_data(request, id):
    """

    NOTE: We do the import as a POST as a security precaution. The
    GET phase isn't really necessary, so the page just POSTs the
    form automatically via JS on load.
    """

    dynmodel = get_object_or_404(models.DynamicModel, id=id)
    if request.method == "GET":
        return render(request, 'import-data.html', {
            "dynmodel": dynmodel
        })
    elif request.method == "POST":
        Model = dynmodel.get_model()

