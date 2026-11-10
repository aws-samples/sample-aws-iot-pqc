// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Dashboard sign-in with the workshop passcode.
//
// The passcode is the password of the single "workshop" user in the
// dashboard's Cognito user pool (created by deploy-workshop.sh), so Cognito
// verifies it. A successful sign-in returns an ID token, used to get AWS
// credentials from the identity pool (guest access is off), and a refresh token
// that keeps the session alive for the workshop day (12 h) without asking again.

import {
  CognitoIdentityProviderClient,
  InitiateAuthCommand,
  RevokeTokenCommand,
} from '@aws-sdk/client-cognito-identity-provider';
import config from './config.js';

export const WORKSHOP_USER = 'workshop';
export const PASSCODE_LENGTH = 8;

const SESSION_KEY = 'pqcDashboard.session';
// Refresh the ID token a little before it expires.
const REFRESH_MARGIN_MS = 5 * 60 * 1000;

const idp = new CognitoIdentityProviderClient({ region: config.region });

/** Thrown when there is no usable session and the user must sign in again. */
export class AuthRequired extends Error {
  constructor() {
    super('Sign-in required');
    this.name = 'AuthRequired';
  }
}

/** Upper-case the typed passcode and keep only letters and digits. */
export function normalizePasscode(value) {
  return (value || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, PASSCODE_LENGTH);
}

function loadSession() {
  try {
    const s = JSON.parse(localStorage.getItem(SESSION_KEY));
    return s && s.refreshToken ? s : null;
  } catch {
    return null;
  }
}

function saveSession(session) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function clearSession() {
  localStorage.removeItem(SESSION_KEY);
}

export function hasSession() {
  return loadSession() !== null;
}

function sessionFrom(result, refreshToken) {
  return {
    idToken: result.IdToken,
    // REFRESH_TOKEN_AUTH does not return a new refresh token; keep the old one.
    refreshToken: result.RefreshToken || refreshToken,
    expiresAt: Date.now() + (result.ExpiresIn || 3600) * 1000,
  };
}

/** A short, actionable message for a failed sign-in. */
export function signInErrorMessage(err) {
  const name = err && err.name;
  const message = (err && err.message) || '';
  if (name === 'NotAuthorizedException' && /attempts exceeded/i.test(message)) {
    return 'Too many wrong passcodes have been tried. Wait a minute, then try again.';
  }
  if (name === 'NotAuthorizedException') {
    return 'That passcode is not right. Check the code on the screen and try again.';
  }
  if (name === 'TooManyRequestsException' || name === 'LimitExceededException') {
    return 'Too many sign-in attempts. Wait a moment, then try again.';
  }
  return 'Sign-in failed. Check your connection and try again.';
}

/** Sign in with the workshop passcode; throws an Error with a user-facing message. */
export async function signIn(passcode) {
  let resp;
  try {
    resp = await idp.send(new InitiateAuthCommand({
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: config.userPoolClientId,
      AuthParameters: { USERNAME: WORKSHOP_USER, PASSWORD: passcode },
    }));
  } catch (err) {
    throw new Error(signInErrorMessage(err));
  }
  if (!resp.AuthenticationResult) {
    // The deploy script sets a permanent password, so no challenge is expected.
    throw new Error('Sign-in needs an extra step this dashboard does not support. Ask your facilitator.');
  }
  saveSession(sessionFrom(resp.AuthenticationResult));
}

/** A valid ID token, refreshed if needed; throws AuthRequired if the session is gone. */
export async function getIdToken() {
  const session = loadSession();
  if (!session) throw new AuthRequired();
  if (session.idToken && Date.now() < session.expiresAt - REFRESH_MARGIN_MS) {
    return session.idToken;
  }
  try {
    const resp = await idp.send(new InitiateAuthCommand({
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: config.userPoolClientId,
      AuthParameters: { REFRESH_TOKEN: session.refreshToken },
    }));
    const next = sessionFrom(resp.AuthenticationResult, session.refreshToken);
    saveSession(next);
    return next.idToken;
  } catch (err) {
    // An expired or revoked refresh token means signing in again; anything else
    // (e.g. a network blip) is retried on the next poll.
    if (err && err.name === 'NotAuthorizedException') {
      clearSession();
      throw new AuthRequired();
    }
    throw err;
  }
}

/** Sign out this browser: forget the session and revoke its refresh token. */
export async function signOut() {
  const session = loadSession();
  clearSession();
  if (session && session.refreshToken) {
    try {
      await idp.send(new RevokeTokenCommand({
        ClientId: config.userPoolClientId,
        Token: session.refreshToken,
      }));
    } catch {
      // Best effort: the session is already gone from this browser.
    }
  }
}
