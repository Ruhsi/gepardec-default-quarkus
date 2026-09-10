package org.acme;

import io.quarkus.hibernate.orm.panache.PanacheRepository;
import org.hibernate.LockMode;
import org.hibernate.LockOptions;
import org.hibernate.Session;
import org.hibernate.query.Query;
import org.hibernate.type.StandardBasicTypes;

import javax.enterprise.context.ApplicationScoped;
import javax.inject.Inject;
import javax.persistence.EntityManager;
import javax.transaction.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@ApplicationScoped
public class PersonRepository implements PanacheRepository<Person> {

    @Inject
    EntityManager entityManager;

    /**
     * Bridge from JPA to the Hibernate 5 Session API.
     */
    public Session getHibernateSession() {
        return entityManager.unwrap(Session.class);
    }

    private Session session() {
        return getHibernateSession();
    }

    /**
     * Hibernate 5 classic Criteria API.
     * In Hibernate 6, Session#createCriteria and org.hibernate.Criteria are gone.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByLastNameUsingClassicCriteria(String lastName) {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Person> cq = cb.createQuery(Person.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        cq.select(root)
                .where(cb.equal(root.get("lastName"), lastName))
                .orderBy(cb.asc(root.get("firstName")));
        return entityManager.createQuery(cq).getResultList();
    }

    /**
     * Old Hibernate 5 uniqueResult pattern.
     */
    @SuppressWarnings("deprecation")
    public Optional<Person> findByEmailUsingClassicCriteria(String email) {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Person> cq = cb.createQuery(Person.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        cq.select(root).where(cb.equal(root.get("email"), email));
        List<Person> results = entityManager.createQuery(cq)
                .setMaxResults(1)
                .getResultList();
        return results.stream().findFirst();
    }

    /**
     * Criteria + MatchMode/ilike is another legacy Hibernate 5 pattern.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findActiveByCityIgnoringCase(String city) {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Person> cq = cb.createQuery(Person.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        javax.persistence.criteria.Predicate activePredicate = cb.equal(root.get("active"), true);
        javax.persistence.criteria.Predicate cityPredicate = cb.equal(
                cb.lower(root.get("city")),
                city.toLowerCase(java.util.Locale.ROOT)
        );
        cq.select(root)
                .where(cb.and(activePredicate, cityPredicate))
                .orderBy(cb.desc(root.get("createdAt")));
        return entityManager.createQuery(cq).getResultList();
    }

    /**
     * Example queries are Hibernate-5 specific and were removed from Hibernate 6.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByExampleUsingHibernateExample(Person probe) {
        java.util.Set<String> excludedProperties = java.util.Set.of("id", "version", "createdAt", "active");
        StringBuilder hql = new StringBuilder("from Person p");
        java.util.List<String> predicates = new java.util.ArrayList<>();
        java.util.Map<String, Object> parameters = new java.util.HashMap<>();

        for (java.lang.reflect.Field field : Person.class.getDeclaredFields()) {
            String propertyName = field.getName();
            if (excludedProperties.contains(propertyName)) {
                continue;
            }

            boolean wasAccessible = field.canAccess(probe);
            field.setAccessible(true);
            Object value;
            try {
                value = field.get(probe);
            } catch (IllegalAccessException e) {
                throw new IllegalStateException("Failed to read probe field: " + propertyName, e);
            } finally {
                field.setAccessible(wasAccessible);
            }

            if (value == null) {
                continue;
            }
            if (value instanceof Number && ((Number) value).doubleValue() == 0d) {
                continue;
            }

            if (value instanceof String) {
                String text = (String) value;
                predicates.add("lower(p." + propertyName + ") like :" + propertyName);
                parameters.put(propertyName, text.toLowerCase(java.util.Locale.ROOT) + "%");
            } else {
                predicates.add("p." + propertyName + " = :" + propertyName);
                parameters.put(propertyName, value);
            }
        }

        if (!predicates.isEmpty()) {
            hql.append(" where ").append(String.join(" and ", predicates));
        }

        hql.append(" order by p.lastName asc, p.firstName asc");
        org.hibernate.query.Query<Person> query = session().createQuery(hql.toString(), Person.class);
        for (java.util.Map.Entry<String, Object> entry : parameters.entrySet()) {
            query.setParameter(entry.getKey(), entry.getValue());
        }
        return query.list();
    }

    /**
     * Legacy Criteria with order + limit.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findLatestActiveUsingClassicCriteria(int maxResults) {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Person> cq = cb.createQuery(Person.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        cq.select(root)
                .where(cb.equal(root.get("active"), true))
                .orderBy(cb.desc(root.get("createdAt")));
        return entityManager.createQuery(cq)
                .setMaxResults(maxResults)
                .getResultList();
    }

    /**
     * Projection API in the old Hibernate 5 Criteria style.
     */
    @SuppressWarnings("deprecation")
    public long countByLastNameUsingProjection(String lastName) {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Long> cq = cb.createQuery(Long.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        cq.select(cb.count(root))
                .where(cb.equal(root.get("lastName"), lastName));
        Long count = entityManager.createQuery(cq).getSingleResult();
        return count == null ? 0L : count;
    }

    /**
     * Projection + ResultTransformer is a classic Hibernate 5 migration hotspot.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Map<String, Object>> findProjectedActivePeople() {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<javax.persistence.Tuple> cq = cb.createTupleQuery();
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        cq.multiselect(
                root.get("id").alias("id"),
                root.get("firstName").alias("firstName"),
                root.get("lastName").alias("lastName"),
                root.get("email").alias("email"),
                root.get("createdAt").alias("createdAt")
        ).where(cb.equal(root.get("active"), true));

        java.util.List<javax.persistence.Tuple> tuples = entityManager.createQuery(cq).getResultList();
        java.util.List<java.util.Map<String, Object>> projected = new java.util.ArrayList<>(tuples.size());
        for (javax.persistence.Tuple tuple : tuples) {
            java.util.Map<String, Object> row = new java.util.LinkedHashMap<>();
            row.put("id", tuple.get("id"));
            row.put("firstName", tuple.get("firstName"));
            row.put("lastName", tuple.get("lastName"));
            row.put("email", tuple.get("email"));
            row.put("createdAt", tuple.get("createdAt"));
            projected.add(row);
        }
        return projected;
    }

    /**
     * SQLRestriction is another old Hibernate-only feature.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findByEmailDomainUsingSqlRestriction(String emailDomain) {
        String pattern = "%@" + emailDomain.toLowerCase(java.util.Locale.ROOT);
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Person> cq = cb.createQuery(Person.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        cq.select(root)
                .where(cb.like(cb.lower(root.get("email")), pattern))
                .orderBy(cb.asc(root.get("email")));
        return entityManager.createQuery(cq).getResultList();
    }

    /**
     * DetachedCriteria + Subqueries are strong examples of Hibernate-5 specific query code.
     */
    @SuppressWarnings({"deprecation", "unchecked"})
    public List<Person> findPeopleCreatedAtLatestTimestamp() {
        javax.persistence.criteria.CriteriaBuilder cb = entityManager.getCriteriaBuilder();
        javax.persistence.criteria.CriteriaQuery<Person> cq = cb.createQuery(Person.class);
        javax.persistence.criteria.Root<Person> root = cq.from(Person.class);
        javax.persistence.criteria.Subquery<Instant> latestCreatedAt = cq.subquery(Instant.class);
        javax.persistence.criteria.Root<Person> subRoot = latestCreatedAt.from(Person.class);
        latestCreatedAt.select(cb.greatest(subRoot.get("createdAt")));
        cq.select(root).where(cb.equal(root.get("createdAt"), latestCreatedAt));
        return entityManager.createQuery(cq).getResultList();
    }

    /**
     * HQL over the native Hibernate Session.
     */
    public List<Person> findCreatedAfterUsingHibernateQuery(Instant createdAfter) {
        Query<Person> query = session().createQuery(
                "from Person p where p.createdAt >= :createdAfter order by p.createdAt desc",
                Person.class
        );

        return query
                .setParameter("createdAfter", createdAfter)
                .list();
    }

    /**
     * Explicit locking via Hibernate Session/LockOptions.
     */
    @Transactional
    public Person findAndLockHibernate(Long id) {
        Person person = session().get(Person.class, id);

        if (person != null) {
            session().buildLockRequest(new LockOptions(LockMode.PESSIMISTIC_WRITE)).lock(person);
        }

        return person;
    }

    /**
     * saveOrUpdate is intentionally Hibernate-centric.
     */
    @Transactional
    public Person saveWithHibernateSession(Person person) {
        session().saveOrUpdate(person);
        session().flush();
        return person;
    }

    /**
     * Hibernate HQL bulk update.
     */
    @Transactional
    public int deactivateByCityUsingBulkHql(String city) {
        Query<?> query = session().createQuery(
                "update Person p set p.active = false where p.city = :city"
        );

        return query
                .setParameter("city", city)
                .executeUpdate();
    }

    /**
     * Hibernate HQL bulk delete.
     */
    @Transactional
    public int deleteInactiveUsingBulkHql() {
        Query<?> query = session().createQuery(
                "delete from Person p where p.active = false"
        );

        return query.executeUpdate();
    }
}