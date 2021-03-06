import io
from datetime import datetime, timedelta

from PIL import Image
from flask import redirect, render_template, request, abort, flash, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from news.lib.access import feed_admin_required, not_banned
from news.clients.amazons3 import S3
from news.clients.db.query import LinkQuery
from news.lib.filters import min_score_filter
from news.lib.pagination import paginate
from news.lib.ratelimit import rate_limit
from news.lib.rss import rss_page
from news.lib.utils.file_type import imagefile
from news.lib.utils.redirect import redirect_back
from news.lib.utils.time_utils import convert_to_timedelta
from news.models.ban import BanForm, Ban
from news.models.feed import FeedForm, EditFeedForm
from news.models.feed_admin import FeedAdmin
from news.models.fully_qualified_source import FullyQualifiedSource
from news.models.link import LinkForm, Link
from news.models.report import Report
from news.models.user import User


@login_required
def new_feed():
    """
    Creates new feed
    :return:
    """
    form = FeedForm()
    if request.method == "POST" and form.validate():
        feed = form.result()
        feed.commit()
        return redirect("/f/{feed}".format(feed=feed.slug))
    return render_template("new_feed.html", form=form)


@not_banned
def get_feed(feed, sort=None):
    """
    Default feed page
    Sort can be specified by user or default to user's preferred sort or to feed default sort
    :param feed: feed
    :param sort: sort
    :return:
    """
    # if (sort is None or sort not in ['trending', 'new', 'best']) and current_user.is_authenticated:
    #    sort = current_user.preferred_sort
    if sort is None:
        sort = feed.default_sort

    ids, has_less, has_more = paginate(
        LinkQuery(feed_id=feed.id, sort=sort).fetch_ids(), 20
    )
    links = Link.by_ids(ids) if len(ids) > 0 else []

    if sort == "new" and current_user.is_authenticated:
        links = filter(min_score_filter(current_user.p_min_link_score), links)

    feed.links = links
    return render_template(
        "feed.html", feed=feed, less_links=has_less, more_links=has_more, sort=sort
    )


@not_banned
def get_feed_rss(feed):
    """
    Returns the feed in rss form
    :param feed: feed
    :return:
    """
    ids, _, _ = paginate(LinkQuery(feed_id=feed.id, sort="trending").fetch_ids(), 30)
    links = Link.by_ids(ids)
    return rss_page(feed, links)


@login_required
@not_banned
def add_link(feed):
    form = LinkForm()
    if request.method == "POST" and form.validate():
        link = form.result()
        link.user_id = current_user.id
        link.feed_id = feed.id
        link.commit()
        current_app.logger.info("new link {} ({})".format(link.title, link.id))
        flash("Link successfully posted", "success")
        return redirect(feed.route)
    return render_template("new_link.html", form=form, feed=feed, md_parser=True)


@feed_admin_required
def remove_link(feed, link_id):
    """
    Removes link from given feed
    This is a hard delete, so be careful
    :param feed: feed
    :param link_slug: link slug
    :return:
    """
    link = Link.by_id(link_id)
    if link is None:
        abort(404)
    link.delete()
    return redirect(redirect_back(feed.route))


@login_required
@not_banned
@rate_limit("subscription", 20, 180, limit_user=True, limit_ip=False)
def subscribe(feed):
    """
    Subscribe user to given feed
    :param feed: feed to subscribe to
    :return:
    """
    current_user.subscribe(feed)
    return redirect(feed.route)


@login_required
@not_banned
@rate_limit("subscription", 20, 180, limit_user=True, limit_ip=False)
def unsubscribe(feed):
    """
    Unsubscribe user from given feed
    :param feed: feed to unsubscribe from
    :return:
    """
    current_user.unsubscribe(feed)
    return redirect(feed.route)


@feed_admin_required
def add_admin(feed):
    """
    Add feed admin
    :param feed: feed
    :return:
    """
    # get new admin username
    username = request.form.get("username")
    if not username or username == "":
        abort(404)

    # check privileges
    if current_user.is_god() or current_user.is_feed_god(feed):
        user = User.by_username(username)
        if user is None:
            abort(404)
        feed_admin = FeedAdmin.create(user_id=user.id, feed_id=feed.id)
        return redirect("{}/admins".format(feed.route))
    else:
        abort(403)


@feed_admin_required
def feed_admins(feed):
    """
    Get all feed admins
    :param feed: feed
    :return:
    """
    admins = FeedAdmin.by_feed_id(feed.id)
    return render_template(
        "feed_admins.html", admins=admins, feed=feed, active_tab="admins"
    )


@feed_admin_required
def GET_feed_admin(feed):
    """
    Get administration page
    User can change rules/description here etc
    :param feed: feed
    :return:
    """
    form = EditFeedForm()
    form.fill(feed)
    return render_template("feed_admin.html", feed=feed, form=form, active_tab="admin")


@feed_admin_required
def POST_feed_admin(feed):
    """
    Handle feed info change
    :param feed: feed
    :return:
    """
    form = EditFeedForm()

    if form.validate():
        needs_update = False

        if feed.description != form.description.data:
            feed.description = form.description.data
            needs_update = True

        if feed.rules != form.rules.data:
            feed.rules = form.rules.data
            needs_update = True

        if form.img.data:
            filename = secure_filename(form.img.data.filename)
            if imagefile(filename):
                img = Image.open(form.img.data)
                if not img.width > 2000 and not img.height > 1000:
                    in_mem_file = io.BytesIO()
                    img.save(in_mem_file, format="PNG")
                    feed.img = "{}.png".format(feed.slug)
                    S3.upload_to_s3(in_mem_file.getvalue(), feed.img)
                    needs_update = True

        if needs_update:
            feed.update_with_cache()

        return redirect("/f/{}/admin".format(feed.slug))

    return render_template("feed_admin.html", feed=feed, form=form, active_tab="admin")


@feed_admin_required
def GET_feed_bans(feed):
    """
    Get bans on given feed
    :param feed: feed
    :return:
    """
    bans = Ban.where("feed_id", feed.id).get()

    return render_template("feed_bans.html", feed=feed, bans=bans, active_tab="bans")


@feed_admin_required
def feed_reports(feed):
    reports = []

    q = request.args.get("q", None)
    if q is not None:
        q_data = q.split(":")
        if len(q_data) != 2:
            abort(404)
        t, d = q_data
        reports = (
            Report.where("feed_id", feed.id)
            .where("reportable_type", "comments" if t == "c" else "links")
            .where("reportable_id", d)
            .order_by("created_at", "desc")
            .get()
        )
    else:
        reports = Report.where("feed_id", feed.id).order_by("created_at", "desc").get()

    return render_template(
        "feed_reports.html", feed=feed, reports=reports, active_tab="reports"
    )


@feed_admin_required
def ban_user(feed, username):
    user = User.by_username(username)
    if user is None:
        abort(404)

    if Ban.by_user_and_feed(user, feed) is not None:
        flash("This user is already baned", "info")
        return redirect(redirect_back(feed.route + "/admin/reports"))

    ban_form = BanForm()
    ban_form.fill(user)

    return render_template("ban_user.html", feed=feed, form=ban_form, user=user)


@feed_admin_required
def post_ban_user(feed, username):
    user = User.by_username(username)
    if user is None:
        abort(404)

    ban_form = BanForm()
    ban_form.fill(user)

    if ban_form.validate():
        expire = datetime.utcnow() + timedelta(seconds=ban_form.get_duration())
        ban = Ban(
            reason=ban_form.reason.data,
            user_id=ban_form.user_id.data,
            feed_id=feed.id,
            until=expire,
        )
        ban.apply()
        return redirect(feed.route + "/bans")

    return render_template("ban_user.html", feed=feed, form=ban_form, user=user)


@feed_admin_required
def feed_fqs(feed):
    sources = FullyQualifiedSource.where("feed_id", feed.id).get()
    return render_template("feed_fqs.html", feed=feed, fqs=sources, active_tab="fqs")


@feed_admin_required
def post_feed_fqs(feed):
    period = convert_to_timedelta(request.form["period"])
    url = request.form["url"]
    if period and url:
        fqs = FullyQualifiedSource(
            url=url, update_interval=period, feed_id=feed.id, next_update=datetime.now()
        )
        fqs.save()
    return redirect("{}/fqs".format(feed.route))


@feed_admin_required
def update_fqs(_, fqs_id):
    source = FullyQualifiedSource.by_id(fqs_id)

    try:
        articles = source.get_links()

        for article in articles:
            # skip if article already posted
            if Link.by_slug(article["slug"]) is not None:
                continue
            link = Link(
                title=article["title"],
                slug=article["slug"],
                text=article["text"],
                url=article["url"],
                feed_id=source.feed_id,
                user_id=12345,
            )
            link.commit()
        source.next_update = datetime.now() + timedelta(seconds=source.update_interval)
        source.save()
    except Exception as e:
        flash("Could not parse the RSS feed on URL".format(source.url), "error")

    return redirect(redirect_back(source.feed.route))


@feed_admin_required
def remove_fqs(_, fqs_id):
    source = FullyQualifiedSource.by_id(fqs_id)
    back = source.feed.route
    if source:
        source.delete()

    return redirect(redirect_back(back))
