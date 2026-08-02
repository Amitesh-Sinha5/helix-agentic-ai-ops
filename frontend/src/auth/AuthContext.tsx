import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { api, onSessionExpired, tokenStore } from '../api';
import type { User } from '../types';

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, fullName?: string) => Promise<void>;
  logout: () => Promise<void>;
  isAdmin: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // Seed from localStorage so a refresh does not flash the login page.
  const [user, setUser] = useState<User | null>(() => tokenStore.user);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!tokenStore.access) {
        setLoading(false);
        return;
      }
      try {
        const fresh = await api.me();
        if (!cancelled) {
          setUser(fresh);
          tokenStore.saveUser(fresh);
        }
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  // The api layer ends the session when a refresh fails; mirror that here.
  useEffect(() => onSessionExpired(() => setUser(null)), []);

  const login = useCallback(async (email: string, password: string) => {
    setUser((await api.login(email, password)).user);
  }, []);

  const signup = useCallback(async (email: string, password: string, fullName?: string) => {
    setUser((await api.signup(email, password, fullName)).user);
  }, []);

  const logout = useCallback(async () => {
    await api.logout();
    setUser(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ user, loading, login, signup, logout, isAdmin: user?.role === 'admin' }),
    [user, loading, login, signup, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside an AuthProvider');
  return context;
}
