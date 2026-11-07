import os
import unittest

# Set isolated configuration before importing the application.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["AUTH_TOKEN_SECRET"] = "unit-test-only-secret-not-for-production"
os.environ["GROQ_API_KEY"] = "unit-test-key"
os.environ["GROQ_MODEL"] = "openai/gpt-oss-20b"

from flask import jsonify, request
from werkzeug.security import generate_password_hash

import secured_app
from app import User, app, db


class SecuredRecommendationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        cls.original_recommend_view = app.view_functions["recommend"]
        app.view_functions["recommend"] = lambda: jsonify(request.get_json())

    @classmethod
    def tearDownClass(cls):
        app.view_functions["recommend"] = cls.original_recommend_view

    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()
            user = User(
                email="student@example.test",
                password_hash="test-hash",
                name="Test Student",
                is_verified=True,
            )
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
        self.client = app.test_client()
        self.token = secured_app.TOKEN_SERIALIZER.dumps({"sub": self.user_id})

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def post_recommend(self, payload=None):
        return self.client.post(
            "/recommend",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload or {"interest": "science", "grades": {}},
        )

    def test_missing_bearer_token_is_rejected(self):
        response = self.client.post("/recommend", json={"interest": "science", "grades": {}})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "authentication_required")

    def test_mismatched_email_does_not_consume_quota(self):
        response = self.post_recommend({"email": "other@example.test", "interest": "science", "grades": {}})
        self.assertEqual(response.status_code, 403)
        with app.app_context():
            self.assertIsNone(db.session.get(secured_app.RecommendationUsage, self.user_id))

    def test_authenticated_user_is_injected(self):
        response = self.post_recommend()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["email"], "student@example.test")
        self.assertEqual(response.get_json()["name"], "Test Student")

    def test_recommendation_usage_has_no_lifetime_limit(self):
        for _ in range(5):
            self.assertEqual(self.post_recommend().status_code, 200)
        with app.app_context():
            usage = db.session.get(secured_app.RecommendationUsage, self.user_id)
            self.assertEqual(usage.attempts, 5)


    def test_unverified_account_is_rejected(self):
        with app.app_context():
            user = db.session.get(User, self.user_id)
            user.is_verified = False
            db.session.commit()
        response = self.post_recommend()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "account_not_verified")

    def test_successful_login_response_receives_signed_token(self):
        with app.test_request_context("/login", method="POST"):
            response = jsonify({"user": {"email": "student@example.test"}})
            response.status_code = 200
            hooked = secured_app.issue_sign_in_token(response)
        self.assertIn("access_token", hooked.get_json())

    def test_multiple_users_receive_independent_tokens_and_recommendations(self):
        users = [
            User(
                email="amina@example.test",
                password_hash=generate_password_hash("correct horse"),
                name="Amina",
                is_verified=True,
                history=[],
            ),
            User(
                email="brian@example.test",
                password_hash=generate_password_hash("correct horse"),
                name="Brian",
                is_verified=True,
                history=[],
            ),
        ]
        with app.app_context():
            db.session.add_all(users)
            db.session.commit()
            user_ids = {user.email: user.id for user in users}

        tokens = {}
        for user in users:
            login = self.client.post(
                "/login",
                json={"email": user.email, "password": "correct horse"},
            )
            self.assertEqual(login.status_code, 200)
            token = login.get_json()["access_token"]
            claims = secured_app.TOKEN_SERIALIZER.loads(token)
            self.assertEqual(claims["sub"], user_ids[user.email])

            recommendation = self.client.post(
                "/recommend",
                headers={"Authorization": f"Bearer {token}"},
                json={"interest": user.name, "grades": {}},
            )
            self.assertEqual(recommendation.status_code, 200)
            self.assertEqual(recommendation.get_json()["email"], user.email)
            self.assertEqual(recommendation.get_json()["name"], user.name)
            tokens[user.email] = token

        self.assertNotEqual(tokens["amina@example.test"], tokens["brian@example.test"])

    def test_options_preflight_does_not_consume_quota(self):
        self.client.options("/recommend")
        with app.app_context():
            self.assertIsNone(db.session.get(secured_app.RecommendationUsage, self.user_id))


if __name__ == "__main__":
    unittest.main()
