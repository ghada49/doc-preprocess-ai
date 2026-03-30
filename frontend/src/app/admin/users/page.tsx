"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { Users, UserPlus, UserX, ShieldCheck } from "lucide-react";
import { listUsers, createUser, deactivateUser } from "@/lib/api/users";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { ConfirmModal } from "@/components/shared/confirm-modal";
import { EmptyState } from "@/components/shared/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBanner } from "@/components/shared/error-banner";
import { getApiErrorMessage } from "@/lib/api/client";
import { formatDate } from "@/lib/utils";
import type { UserRecord } from "@/types/api";
import { cn } from "@/lib/utils";

export default function UsersPage() {
  const queryClient = useQueryClient();

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["users"],
    queryFn: listUsers,
    staleTime: 30_000,
  });

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [deactivateTarget, setDeactivateTarget] = useState<UserRecord | null>(null);

  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<"user" | "admin">("user");

  const createMut = useMutation({
    mutationFn: () => createUser({ username: newUsername, password: newPassword, role: newRole }),
    onSuccess: (user) => {
      toast.success(`User "${user.username}" created.`);
      queryClient.invalidateQueries({ queryKey: ["users"] });
      setShowCreateModal(false);
      setNewUsername(""); setNewPassword(""); setNewRole("user");
    },
    onError: (err: unknown) => {
      toast.error(getApiErrorMessage(err, "Failed to create user."));
    },
  });

  const deactivateMut = useMutation({
    mutationFn: (userId: string) => deactivateUser(userId),
    onSuccess: (user) => {
      toast.success(`User "${user.username}" deactivated.`);
      queryClient.invalidateQueries({ queryKey: ["users"] });
      setDeactivateTarget(null);
    },
    onError: (err: unknown) => {
      toast.error(getApiErrorMessage(err, "Failed to deactivate user."));
    },
  });

  const users = data?.items ?? [];

  return (
    <AdminShell breadcrumbs={[{ label: "Users" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="User Management"
          description="Create, view, and deactivate user accounts."
          icon={Users}
          iconColor="text-indigo-600"
          actions={
            <Button size="sm" onClick={() => setShowCreateModal(true)} className="gap-1.5">
              <UserPlus className="h-3.5 w-3.5" />
              New User
            </Button>
          }
        />

        {isError && (
          <ErrorBanner message={getApiErrorMessage(error, "Failed to load users.")} />
        )}

        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th>Username</th>
                <th>Role</th>
                <th>Status</th>
                <th>Created</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i} className="border-b border-slate-100">
                    {[120, 80, 70, 120, 80].map((w, j) => (
                      <td key={j} className="px-4 py-3.5">
                        <Skeleton className="h-4" style={{ width: w }} />
                      </td>
                    ))}
                  </tr>
                ))
              ) : users.length === 0 ? (
                <tr>
                  <td colSpan={5} className="p-0">
                    <EmptyState
                      icon={Users}
                      title="No users yet"
                      description="Create the first user to get started."
                    />
                  </td>
                </tr>
              ) : (
                users.map((user) => (
                  <tr key={user.user_id} className="hover:bg-slate-50">
                    <td>
                      <div className="flex items-center gap-2">
                        <div className="flex h-7 w-7 items-center justify-center rounded-full bg-indigo-100 border border-indigo-200">
                          <span className="text-xs font-semibold text-indigo-600">
                            {user.username.charAt(0).toUpperCase()}
                          </span>
                        </div>
                        <span className="text-sm text-slate-800 font-medium">{user.username}</span>
                      </div>
                    </td>
                    <td>
                      <div className="flex items-center gap-1.5">
                        {user.role === "admin" && (
                          <ShieldCheck className="h-3.5 w-3.5 text-indigo-600" />
                        )}
                        <Badge variant={user.role === "admin" ? "info" : "muted"}>
                          {user.role}
                        </Badge>
                      </div>
                    </td>
                    <td>
                      <Badge variant={user.is_active ? "success" : "muted"} dot>
                        {user.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </td>
                    <td>
                      <span className="text-xs text-slate-500">{formatDate(user.created_at)}</span>
                    </td>
                    <td className="text-right">
                      {user.is_active && (
                        <Button
                          variant="danger"
                          size="xs"
                          onClick={() => setDeactivateTarget(user)}
                          className="gap-1"
                        >
                          <UserX className="h-3 w-3" />
                          Deactivate
                        </Button>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Create user modal */}
      <Dialog open={showCreateModal} onOpenChange={setShowCreateModal}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Create User</DialogTitle>
          </DialogHeader>
          <div className="px-6 space-y-4">
            <div className="space-y-1.5">
              <Label>Username</Label>
              <Input value={newUsername} onChange={(e) => setNewUsername(e.target.value)} placeholder="alice" />
            </div>
            <div className="space-y-1.5">
              <Label>Password</Label>
              <Input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="••••••••" />
            </div>
            <div className="space-y-1.5">
              <Label>Role</Label>
              <Select value={newRole} onValueChange={(v) => setNewRole(v as "user" | "admin")}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="user">User</SelectItem>
                  <SelectItem value="admin">Admin</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setShowCreateModal(false)}>Cancel</Button>
            <Button
              size="sm"
              onClick={() => createMut.mutate()}
              loading={createMut.isPending}
              disabled={!newUsername.trim() || !newPassword.trim()}
            >
              Create User
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Deactivate confirmation */}
      <ConfirmModal
        open={!!deactivateTarget}
        onOpenChange={(open) => { if (!open) setDeactivateTarget(null); }}
        title={`Deactivate "${deactivateTarget?.username}"?`}
        description="This user will no longer be able to log in. This action can be reversed by an administrator."
        confirmLabel="Deactivate"
        variant="danger"
        loading={deactivateMut.isPending}
        onConfirm={() => {
          if (deactivateTarget) deactivateMut.mutate(deactivateTarget.user_id);
        }}
      />
    </AdminShell>
  );
}
